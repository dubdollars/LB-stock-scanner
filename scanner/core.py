"""
Minervini/O'Neil SEPA scanner — core pipeline (v2).

Significant upgrades over the original notebook:
  - VDU is no longer a hard filter (high-volume breakouts shouldn't be excluded)
  - Smarter pivot detection (real swing-high logic, not tail(10).max())
  - Smarter stop placement (tighter of: pivot-1ATR, swing low, pivot*0.93)
  - Real VCP characterization (counts contractions and depths)
  - Power Play setup detection
  - Breakout-today flag (close > pivot on volume > 1.5x avg)
  - Extension-from-50MA warning
  - Earnings proximity warning
  - Industry group RS ranking
  - Universe expanded with iShares Russell 3000 (IWV) holdings
  - EXCLUDED_TICKERS dropped — relies on data validation instead
"""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from io import StringIO
from typing import Callable, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from .setups import (
    calc_atr,
    detect_breakout_today,
    detect_cheat_setup,
    detect_extension_from_50ma,
    detect_gap_up_today,
    detect_ma_target,
    detect_power_play,
    detect_vcp_contractions,
    find_pivot_point,
    calculate_smart_stop,
    vcp_quality_score,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
warnings.simplefilter(action="ignore", category=FutureWarning)
warnings.simplefilter(action="ignore", category=pd.errors.PerformanceWarning)

SECTOR_ABBREV = {
    "Technology": "Tech",
    "Healthcare": "Health",
    "Financial Services": "Finance",
    "Consumer Cyclical": "Cons.Cyc",
    "Consumer Defensive": "Cons.Def",
    "Communication Services": "Comm.Svc",
    "Basic Materials": "Materials",
    "Real Estate": "RE",
    "Industrials": "Indust.",
    "Energy": "Energy",
    "Utilities": "Utilities",
}

ProgressCallback = Optional[Callable[[str, int, int, str], None]]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScanSettings:
    account_size: float = 10_000.0
    risk_per_trade: float = 0.0125
    max_stop_allowed: float = 0.10
    elite_mode: bool = True
    min_price: float = 5.00
    min_avg_volume: int = 100_000
    # Optional: restrict scan to a subset of tickers (for demo / debugging).
    ticker_subset: Optional[list[str]] = None


@dataclass
class ScanResult:
    """Everything the dashboard needs from one scan."""
    timestamp: str
    settings: dict
    market_signal: str
    sp_trend: str
    nq_trend: str
    rut_trend: str
    sp_dist: int
    nq_dist: int
    rut_dist: int
    universe_size: int
    pct_above_50: float
    pct_above_200: float
    up_down_ratio: float
    new_highs: int
    new_lows: int
    breadth_status: str
    pct_above_50_10d_ago: float = 0.0
    narrowing: bool = False
    rs_threshold: int = 70
    mode_label: str = "GRAND MASTER"
    candidates: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    skipped_history: int = 0
    skipped_liquidity: int = 0
    industry_ranks: dict = field(default_factory=dict)  # industry -> rank (1=best)
    # Full-universe industry rollup — list of {Ticker, RS, Industry, Sector}
    # for every RS-rated ticker that has cached fundamentals. Powers the
    # Rotation page's industry table (gives it ~70 industries instead of
    # only the ~20 that have 3+ SEPA-passing candidates).
    industry_universe: list = field(default_factory=list)
    # Distribution-day DATES per index (ISO YYYY-MM-DD strings, oldest first,
    # after O'Neil erasure). Added 2026-07-10 so the market-signal banner
    # can show hover/click reveal of exactly which days were counted.
    # Placed at the END of the dataclass because Python requires all fields
    # with defaults to come AFTER all non-defaulted fields (universe_size,
    # sp_dist, nq_dist, rut_dist are all non-defaulted). Legacy scans
    # loaded from disk get [] via default_factory — the UI shows a "re-run
    # to capture" hint in the expander for those.
    sp_dist_dates: list = field(default_factory=list)
    nq_dist_dates: list = field(default_factory=list)
    rut_dist_dates: list = field(default_factory=list)
    # Market state machine snapshot — O'Neil correction/rally/FTD tracking.
    # See detect_market_state_and_ftds() for the full contract. Legacy
    # scan files that predate this field load with empty dict.
    market_state: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def safe_get_info(ticker_obj, retries: int = 3, delay: float = 2.0) -> dict:
    """Fetch .info with retries to handle transient Yahoo Finance failures."""
    for _ in range(retries):
        try:
            info = ticker_obj.info
            if info and len(info) > 5:
                return info
        except Exception:
            pass
        time.sleep(delay)
    return {}


def clean_history(df: pd.DataFrame) -> pd.DataFrame:
    """Strip today's potentially incomplete intraday bar."""
    if df is None or len(df) == 0:
        return df
    now_utc = pd.Timestamp.utcnow()
    last_bar = df.index[-1]
    if last_bar.tzinfo is None:
        last_bar_utc = last_bar.tz_localize("UTC")
    else:
        last_bar_utc = last_bar.tz_convert("UTC")
    if last_bar_utc.date() == now_utc.date():
        df = df.iloc[:-1]
    return df


def calc_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    try:
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except Exception:
        return None


def format_mktcap(val) -> str:
    try:
        val = float(val)
        if val >= 1_000_000_000_000:
            return f"${val/1_000_000_000_000:.1f}T"
        elif val >= 1_000_000_000:
            return f"${val/1_000_000_000:.1f}B"
        elif val >= 1_000_000:
            return f"${val/1_000_000:.0f}M"
        else:
            return f"${val:,.0f}"
    except Exception:
        return "N/A"


def days_until(date_str: Optional[str]) -> Optional[int]:
    """Days from today until the given YYYY-MM-DD string. None if unparseable."""
    if not date_str:
        return None
    try:
        target = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        return (target - today).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 0: Market context
# ---------------------------------------------------------------------------

def diagnose_market_context() -> tuple:
    """Return (market_signal, sp_trend, sp_dist, nq_trend, nq_dist,
    rut_trend, rut_dist, sp_dates, nq_dates, rut_dates, state_info).

    Distribution dates are ISO YYYY-MM-DD strings, oldest first. Uses strict
    IBD rules (down 0.2%+ on volume higher than prior day). Data source is
    ETF proxies (SPY / QQQ / IWM) not underlying indices (^GSPC / ^IXIC /
    ^RUT) because yfinance's index-level volume series is notoriously
    inconsistent — ETF share volume is clean and matches institutional flow.
    Prices tracked by the ETFs move ~1:1 with the underlying indices so the
    price test is unaffected.

    state_info is a dict describing the O'Neil correction/rally-attempt/
    confirmed-uptrend state machine — see detect_market_state_and_ftds()
    for the full contract.
    """
    market_signal = "GREEN (Aggressive)"
    sp_trend = nq_trend = rut_trend = "N/A"
    sp_dist = nq_dist = rut_dist = 0
    sp_dates: list[str] = []
    nq_dates: list[str] = []
    rut_dates: list[str] = []
    state_info: dict = {}

    try:
        # ETF proxies for market-state analysis. SPY = S&P 500, QQQ =
        # Nasdaq-100 (institutional side of the tape; better proxy for
        # what moves markets than the noisy Nasdaq Composite), IWM =
        # Russell 2000. All three have clean, consistent share volume
        # that we can trust for prior-day comparisons.
        indices = yf.download(
            ["SPY", "QQQ", "IWM"], period="80d", progress=False
        )

        def check_index(ticker):
            """Return (trend, dist_count, dist_date_list) — strict IBD
            Market Pulse rules.

            Distribution day = index closes down 0.2%+ from prior close,
            AND today's volume is HIGHER than yesterday's volume.

            The count is the number of such days in the trailing 25
            trading sessions. Days age out of the count naturally as the
            25-session window slides forward — no separate "erasure" loop.

            IMPORTANT — what changed 2026-07-22 (user-reported bug fix):
            The previous implementation used "volume > 50-day average"
            instead of "volume > prior day". That single deviation from
            O'Neil / IBD's published rule caused three visible problems:

              1. False positives around earnings/expiration weeks. Days
                 with heavy background volume (quad witching, quarter-end)
                 inflated the 50-day average, but a subsequent slightly-
                 lower-volume down-day still beat the average and got
                 flagged. Under the correct prior-day test it fails
                 because expiration Friday's volume is huge.

              2. False negatives on real distribution days like July 13
                 and July 17. Their absolute volume was elevated, but
                 the 50-day average had been dragged UP by even bigger
                 panic days earlier in the window, so they failed the
                 average test. Under the correct prior-day test they
                 easily pass.

              3. Retroactive count changes ("yesterday said 4, today
                 says 5, no new day was added"). The 50-day average
                 changes every day as the window slides, so a borderline
                 historical day could flip from "not counted" to "counted"
                 without anything new happening. Under the prior-day test,
                 each historical day's flag status is FROZEN — determined
                 only by its own volume vs. the immediately prior day, both
                 of which are historical constants. This eliminates the
                 whole class of "the past changed" bugs.

            The rally-cancellation ("1.7%+ up-day erases a dist day")
            logic is also removed. It has no basis in IBD's actual
            published methodology — IBD ages days out via the 25-session
            window and doesn't credit accumulation days as cancellations.
            """
            try:
                try:
                    closes = indices["Close"][ticker]
                    vols = indices["Volume"][ticker]
                except Exception:
                    closes = indices["Close"]
                    vols = indices["Volume"]

                dist_dates: list[str] = []
                # Walk the last 25 trading sessions. For each day i:
                #   - Compare close[i] vs close[i-1] for the price test
                #   - Compare volume[i] vs volume[i-1] for the volume test
                # Both are pure historical comparisons — no rolling window,
                # no average, no state that changes as new bars arrive.
                # This is what makes the count deterministic and stable
                # across scans.
                lookback_start = max(1, len(closes) - 25)
                for i in range(lookback_start, len(closes)):
                    try:
                        prev_close = float(closes.iloc[i - 1])
                        cur_close = float(closes.iloc[i])
                        prev_vol = float(vols.iloc[i - 1])
                        cur_vol = float(vols.iloc[i])
                    except Exception:
                        # NaN or missing bar — skip it. yfinance occasionally
                        # returns partial data for the current session; better
                        # to skip than to falsely flag.
                        continue
                    if prev_close <= 0 or prev_vol <= 0:
                        continue  # bad data; skip
                    pct = (cur_close - prev_close) / prev_close
                    if pct <= -0.002 and cur_vol > prev_vol:
                        try:
                            iso = closes.index[i].strftime("%Y-%m-%d")
                        except Exception:
                            iso = str(closes.index[i])[:10]
                        dist_dates.append(iso)

                # Trend labels use ">50MA" / "<50MA" so it's unambiguous what
                # they measure. Older labels "UP" / "DOWN" implied "up on the
                # day" which is a different thing (Russell could be down -0.7%
                # on the day but still trading above its 50-day MA, which
                # confused users into thinking the scanner was miscounting).
                # This label matches Minervini's actual trend template: price
                # above/below the medium-term MA is the trend definition.
                trend = ">50MA" if closes.iloc[-1] > closes.tail(50).mean() else "<50MA"
                return trend, len(dist_dates), dist_dates
            except Exception:
                return "N/A", 0, []

        sp_trend, sp_dist, sp_dates = check_index("SPY")
        nq_trend, nq_dist, nq_dates = check_index("QQQ")
        rut_trend, rut_dist, rut_dates = check_index("IWM")

        max_dist = max(sp_dist, nq_dist)
        if max_dist >= 6:
            market_signal = "RED (Heavy Distribution)"
        elif max_dist >= 4:
            market_signal = "YELLOW (Caution)"

        # Trend labels renamed 2026-07-22 from "UP"/"DOWN" to ">50MA"/"<50MA"
        # so users don't misread them as "up on the day". The semantic
        # meaning is unchanged — trend = price vs 50-day MA — so all the
        # market-signal downgrade rules below still apply, just with new
        # string literals.
        if sp_trend == "<50MA" or nq_trend == "<50MA":
            if market_signal == "GREEN (Aggressive)":
                market_signal = "YELLOW (Trend Broken)"

        if sp_trend == ">50MA" and rut_trend == "<50MA":
            if "GREEN" in market_signal:
                market_signal = "YELLOW (Divergence - Small Caps Lagging)"

        # ---- State machine + FTD detection ----
        # SPY drives the state (correction/rally-attempt) because it's the
        # cleanest "market" proxy. FTDs are detected on any of the three
        # ETFs — any one qualifying triggers a state transition to UPTREND.
        state_info = detect_market_state_and_ftds(indices, sp_dates)
    except Exception:
        pass

    return (market_signal, sp_trend, sp_dist, nq_trend, nq_dist, rut_trend, rut_dist,
            sp_dates, nq_dates, rut_dates, state_info)


# ---------------------------------------------------------------------------
# Market state machine (O'Neil correction / rally-attempt / FTD)
# ---------------------------------------------------------------------------

# FTD price threshold. IBD's current published threshold is 1.4%; O'Neil's
# original threshold from *How to Make Money in Stocks* was 1.7%. Some modern
# practitioners use 1.25% to catch borderline confirmations that IBD misses.
# User set to 1.25% (2026-07-22) after asking about how often 1.4% actually
# prints on major indices — the answer is "not often enough in sleepy tapes."
# Days above 1.7% are noted separately as "strong FTDs" for the UI.
_FTD_PCT_THRESHOLD = 0.0125
_FTD_STRONG_THRESHOLD = 0.017

# Minimum day-of-rally-attempt for a valid FTD. O'Neil's rule: Day 4+.
# FTDs on Day 1-3 are considered premature (rally hasn't established
# enough follow-through to distinguish from a dead-cat bounce).
_FTD_MIN_DAY = 4

# Distribution-days threshold that flips UPTREND -> CORRECTION. This
# mirrors the existing "YELLOW (Caution)" signal cutoff so the state
# machine and the market-signal banner stay directionally aligned.
_CORRECTION_DIST_THRESHOLD = 5


def detect_market_state_and_ftds(indices_df, sp_dist_dates: list) -> dict:
    """Walk forward through the price history for SPY/QQQ/IWM and produce
    a snapshot of the O'Neil market-state machine.

    Returns a dict with:
      state             — "UPTREND" | "CORRECTION" | "RALLY_ATTEMPT"
      state_since       — ISO date the current state began
      correction_low    — {"date", "close"} of the lowest SPY close inside
                          the current correction/rally-attempt; None if
                          currently UPTREND
      rally_day         — Day-N of the current rally attempt (1-based), or
                          0 if not currently in a rally attempt
      last_ftd          — {"date", "index", "pct_gain", "vol_ratio"} of the
                          most recent FTD in the window; None if none
      ftd_history       — list of every FTD detected in the window, oldest
                          first. Same shape as last_ftd.

    State transitions (walked chronologically, deterministic):

      UPTREND
        └─ CORRECTION triggered when SPY dist-days in trailing 25 sessions
           reach _CORRECTION_DIST_THRESHOLD (5). Marks correction_low =
           SPY close on trigger day.

      CORRECTION
        ├─ correction_low updated on any new lower close.
        └─ RALLY_ATTEMPT triggered on the first up-day (SPY close > prior
           close) after a fresh lower low. Day 1 = that up-day.

      RALLY_ATTEMPT
        ├─ Day counter advances each session.
        ├─ Reset to CORRECTION if SPY closes below correction_low
           (attempt failed; next up-day starts a new attempt).
        └─ UPTREND triggered when ANY of the three ETFs prints an FTD
           on Day 4+ of the current attempt. FTD = close +1.25%+ on
           volume higher than prior session.

    The walk starts from the earliest usable bar (index 25+ so we have
    enough history to count dist-days for the correction trigger). If the
    market is already deep in correction at the start of the window, the
    machine catches up on its own within a few sessions.
    """
    result = {
        "state": "UPTREND",
        "state_since": "",
        "correction_low": None,
        "rally_day": 0,
        "last_ftd": None,
        "ftd_history": [],
    }

    try:
        # Extract SPY (state driver) + all three ETFs (FTD checker).
        # yfinance's multi-ticker download returns a MultiIndex; the
        # try/except handles the single-ticker fallback shape.
        try:
            spy_closes = indices_df["Close"]["SPY"]
            spy_vols   = indices_df["Volume"]["SPY"]
            qqq_closes = indices_df["Close"]["QQQ"]
            qqq_vols   = indices_df["Volume"]["QQQ"]
            iwm_closes = indices_df["Close"]["IWM"]
            iwm_vols   = indices_df["Volume"]["IWM"]
        except Exception:
            return result

        n = len(spy_closes)
        if n < 30:
            return result  # not enough history to run state machine safely

        # Pre-compute distribution-day dates on SPY across the full window
        # so we can quickly ask "how many dist-days in the trailing 25
        # sessions ending at index i?" during the walk.
        sp_dist_iso_set = set(sp_dist_dates or [])

        def _iso(idx):
            try:
                return idx.strftime("%Y-%m-%d")
            except Exception:
                return str(idx)[:10]

        def _pct(series, i):
            try:
                prev = float(series.iloc[i - 1])
                cur = float(series.iloc[i])
                if prev <= 0:
                    return None
                return (cur - prev) / prev
            except Exception:
                return None

        def _vol_ratio(vols, i):
            try:
                prev = float(vols.iloc[i - 1])
                cur = float(vols.iloc[i])
                if prev <= 0:
                    return None
                return cur / prev
            except Exception:
                return None

        # Count SPY dist-days in trailing 25 sessions ending at index i.
        # Fast: iterate window, check membership in the pre-built ISO set.
        def _dist_days_in_trailing_25(end_i: int) -> int:
            start = max(0, end_i - 25 + 1)
            count = 0
            for k in range(start, end_i + 1):
                if _iso(spy_closes.index[k]) in sp_dist_iso_set:
                    count += 1
            return count

        # State machine walk. Start at index 1 (need i-1 for comparisons)
        # but the CORRECTION trigger needs at least 25 sessions of history
        # to be meaningful, so we start UPTREND until index 25.
        state = "UPTREND"
        state_since_i = 0
        correction_low: dict | None = None  # {"date": iso, "close": float}
        rally_day = 0

        for i in range(1, n):
            date_iso = _iso(spy_closes.index[i])
            spy_pct = _pct(spy_closes, i)
            spy_close = float(spy_closes.iloc[i]) if spy_closes.iloc[i] == spy_closes.iloc[i] else None
            if spy_pct is None or spy_close is None:
                continue

            if state == "UPTREND":
                # Trigger CORRECTION if trailing dist-days accumulated.
                if i >= 25 and _dist_days_in_trailing_25(i) >= _CORRECTION_DIST_THRESHOLD:
                    state = "CORRECTION"
                    state_since_i = i
                    correction_low = {"date": date_iso, "close": spy_close}
                    rally_day = 0

            elif state == "CORRECTION":
                # Track new lows.
                if correction_low is None or spy_close < correction_low["close"]:
                    correction_low = {"date": date_iso, "close": spy_close}
                # First up-day starts a rally attempt.
                if spy_pct > 0:
                    state = "RALLY_ATTEMPT"
                    state_since_i = i
                    rally_day = 1

            elif state == "RALLY_ATTEMPT":
                # Advance day counter (Day 1 was set on the transition day;
                # subsequent sessions advance).
                rally_day += 1

                # Failure: SPY closed below the correction low → attempt
                # resets. New correction_low is today's close. Machine
                # returns to CORRECTION; next up-day starts new attempt.
                if correction_low is not None and spy_close < correction_low["close"]:
                    state = "CORRECTION"
                    state_since_i = i
                    correction_low = {"date": date_iso, "close": spy_close}
                    rally_day = 0
                    continue

                # FTD detection — only valid from Day 4+.
                if rally_day >= _FTD_MIN_DAY:
                    # Check each ETF; any qualifying day triggers FTD.
                    # If multiple qualify same day, the strongest wins in
                    # the record for last_ftd (higher pct gain).
                    ftd_candidates = []
                    for label, closes, vols in [
                        ("S&P 500", spy_closes, spy_vols),
                        ("Nasdaq",  qqq_closes, qqq_vols),
                        ("Russell 2000", iwm_closes, iwm_vols),
                    ]:
                        pct = _pct(closes, i)
                        vr  = _vol_ratio(vols, i)
                        if pct is None or vr is None:
                            continue
                        if pct >= _FTD_PCT_THRESHOLD and vr > 1.0:
                            ftd_candidates.append({
                                "date": date_iso,
                                "index": label,
                                "pct_gain": round(pct * 100, 2),
                                "vol_ratio": round(vr, 2),
                                "strong": pct >= _FTD_STRONG_THRESHOLD,
                                "rally_day": rally_day,
                            })
                    if ftd_candidates:
                        # Sort by pct_gain desc; the winner represents "the"
                        # FTD for this session, but we log all of them so
                        # the user can see confirmations across indices.
                        ftd_candidates.sort(key=lambda x: -x["pct_gain"])
                        result["ftd_history"].extend(ftd_candidates)
                        # Transition to UPTREND. state_since = today.
                        state = "UPTREND"
                        state_since_i = i
                        correction_low = None
                        rally_day = 0

        # Materialize the final state.
        result["state"] = state
        result["state_since"] = _iso(spy_closes.index[state_since_i]) if state_since_i else _iso(spy_closes.index[0])
        result["correction_low"] = correction_low
        result["rally_day"] = rally_day if state == "RALLY_ATTEMPT" else 0
        if result["ftd_history"]:
            result["last_ftd"] = result["ftd_history"][-1]
        return result
    except Exception:
        return result


# ---------------------------------------------------------------------------
# Phase 1: Ticker collection (universe = S&P 500 + Nasdaq 100 + Russell 3000)
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------------
# Hardcoded fallback index membership.
#
# When Wikipedia is down, blocked, or its table layout changes, the live
# fetches return empty sets and every ticker silently gets labeled "RUT"
# (because RUT is the else-branch). These fallbacks ensure the major US
# mega-caps still get labeled correctly even when the live source fails.
# Updated approximately quarterly — exact membership drifts but the largest
# names are stable for years.
# ---------------------------------------------------------------------------

_STATIC_SP500_FALLBACK = {
    # Mega caps (top 100 by weight, stable for years)
    "AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","BRK-B","JPM",
    "V","MA","UNH","JNJ","PG","XOM","HD","ABBV","BAC","AVGO","KO","PEP",
    "COST","MRK","WMT","CSCO","ACN","ADBE","NFLX","CRM","ORCL","AMD","TMO",
    "ABT","LIN","INTC","CMCSA","NKE","MCD","DHR","WFC","DIS","AMGN","CVX",
    "PFE","NEE","RTX","INTU","IBM","GE","HON","UNP","LOW","BMY","GS","ELV",
    "AXP","T","UPS","BA","NOW","MS","CAT","BX","DE","BLK","GILD","BKNG",
    "ISRG","AMAT","SPGI","MDT","SCHW","TJX","PLD","MDLZ","REGN","LRCX",
    "SBUX","VRTX","MMC","KLAC","PANW","ETN","BSX","ZTS","CB","SNPS","CDNS",
    "ADP","FI","AMT","NOC","ITW","ICE","MO","USB","CL","GD","FDX","MCK",
    "EOG","PYPL","MAR","EMR","EW","AON","MNST","AJG","BDX","CME","COF",
    "ROP","SLB","FCX","OKE","JCI","NSC","CI","PSA","MRVL","APD","GM","F",
    "KMI","MET","FTNT","AZO","CTAS","KMB","COR","CHTR","ALL","HLT","DLR",
    "PCG","EXC","KDP","OXY","KHC","CMG","EL","FIS","LULU","ROST","IR","AIG",
    "PRU","ORLY","IDXX","AME","GIS","ECL","MSI","CTSH","AFL","VICI","NXPI",
    "BKR","RSG","NUE","NEM","MPC","A","FAST","OTIS","EA","CARR","PCAR","SYY",
    "WELL","CSX","EBAY","EQIX","TRV","EXR","COP","VLO","PSX","AEP","PEG",
    "WMB","AWK","GPC","FANG","TT","CMI","ADM","LHX","BIIB","PWR","MLM",
    "CPRT","KEYS","CRWD","ROK","AMP","TGT","AVB","IQV","KR","ALB","STZ",
    "DXCM","WST","DD","HPQ","DVN","CHD","HSY","HUM","STT","ANSS","TRGP",
    "MTB","FITB","RCL","CCL","NDAQ","CDW","DOV","HCA","ZBH","HAL","MSCI",
    "BR","RMD","TROW","TSN","WAB","FCNCA","DG","DLTR","DOW","TYL","WBA",
    "NTRS","WAT","KEY","HBAN","RJF","CFG","CINF","HIG","INVH","UDR","ESS",
    "MAA","ARE","WY","BXP","VTR","DTE","FE","ETR","ATO","CMS","NI","SO",
    "DUK","D","SRE","XEL","WEC","AEE","PPL","ES","EIX","CNP",
}

_STATIC_NQ100_FALLBACK = {
    # Nasdaq 100 stable members (heavy overlap with S&P 500 mega-caps)
    "AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","AVGO","COST",
    "NFLX","ADBE","PEP","CSCO","TMUS","CMCSA","INTC","INTU","AMD","TXN",
    "QCOM","AMGN","HON","SBUX","ISRG","BKNG","GILD","MDLZ","REGN","ADI",
    "ADP","VRTX","LRCX","KLAC","PANW","SNPS","CDNS","CHTR","MAR","ABNB",
    "MELI","ASML","ORLY","PYPL","FTNT","CTAS","CSX","MNST","MRVL","ATVI",
    "AEP","PCAR","ROST","ODFL","KDP","EXC","FAST","KHC","BIIB","DXCM",
    "IDXX","CTSH","DDOG","CRWD","WBD","WBA","TEAM","ZS","ZM","ALGN",
    "EA","ANSS","CDW","XEL","NXPI","SIRI","VRSK","ILMN","ON","MCHP",
    "GFS","CEG","DLTR","WDAY","SGEN","MRNA","ENPH","LULU","BKR","MTCH",
    "PDD","CCEP","ARM","SMCI","DASH","TTD","ROP","FANG","DDOG","CSGP",
}


# Curated Russell 2000/3000 fallback (~260 mid/small-caps). Not exhaustive —
# Russell 2000 has 2000 names and changes annually — but covers the most
# frequently-mentioned mid-caps that would otherwise drop out of the universe
# entirely if iShares is unreachable.
_STATIC_RUT_FALLBACK = {
    # Mid-cap tech
    "PLTR","SNOW","CRWD","DDOG","MDB","NET","ZS","OKTA","SMCI","ARM","DASH",
    "U","RBLX","COIN","HOOD","SOFI","UPST","AFRM","SQ","PYPL","SHOP",
    "TWLO","ESTC","CFLT","S","FROG","BILL","PATH","AI","C3AI","BBAI",
    "AMBA","CYBR","TENB","RPD","SAIL","FTNT","PANW","PRGS","DOCN","FSLY",
    "AKAM","CDW","NTAP","WDC","STX","LRCX","KLAC","AMAT","ASML","MU",
    "ON","MPWR","ENTG","TER","COHR","IPGP","FORM","ICHR","ACLS","KLIC",
    "AMKR","UCTT","BSY","PRO","NEWR","DT","VRNS","TDOC","SVMK","DOMO",
    # Mid-cap biotech / healthcare
    "REGN","VRTX","BIIB","ALNY","BMRN","NBIX","UTHR","BPMC","FOLD","SAGE",
    "EXEL","INCY","JAZZ","RPRX","HALO","ARWR","AXSM","CRSP","NTLA","BEAM",
    "EDIT","VERV","MRTX","RNA","SRPT","KRYS","ALKS","XENE","RYTM","NUVL",
    "LIVN","TXG","TFX","NVST","ICUI","HOLX","RMD","ALGN","DXCM","PODD",
    "GMED","INSP","NVRO","STAA","TMDX","PRCT","SILK","CDMO","CRL","BIO",
    # Mid-cap financials
    "SCHW","NDAQ","CBOE","MKTX","TROW","BEN","IVZ","LPLA","HOOD","RJF",
    "CG","KKR","APO","ARES","BX","BLK","STT","NTRS","BK","STIFEL","SF",
    "WAL","CMA","FHN","ZION","CFG","HBAN","FITB","KEY","RF","MTB","SBNY",
    "PNFP","WTFC","PB","UMBF","CBSH","BKU","EWBC","BOKF","ASB","HOMB",
    # Mid-cap industrials / energy
    "TREX","BLD","TPR","RH","WSM","DECK","CROX","BIRK","ONON","LULU",
    "AVAV","KTOS","HEI","TDG","HXL","CW","WWD","MOG-A","RBC","ESLT",
    "FCN","HURN","ICFI","BAH","SAIC","CACI","LDOS","MAXR","LMT","NOC",
    "LNG","TPL","FANG","DVN","MRO","APA","PR","MTDR","RIG","HP","NOV",
    "FTI","CHX","LBRT","BKR","NEX","WTTR","ETRN","ARLP","BTU","HCC",
    # Mid-cap consumer / retail
    "ELF","BBWI","ULTA","FIVE","CHWY","WING","CMG","TXRH","CAKE","BLMN",
    "SHAK","CAVA","PLAY","JACK","WEN","DPZ","QSR","DRI","EAT","BJRI",
    "PLNT","XPOF","LULU","ANF","URBN","AEO","BURL","ROST","TJX","DKS",
    "GPS","KSS","JWN","M","DDS","NWL","HBI","CRI","COLM","KTB",
    # Mid-cap real estate / utilities / materials
    "PSA","EXR","CUBE","NSA","SUI","ELS","MAA","CPT","UDR","ESS","AVB",
    "EQR","INVH","AMH","FRT","REG","KIM","BRX","O","STAG","REXR","FR",
    "STZ","TAP","SAM","FIZZ","COKE","KO","PEP","KDP","MNST","CELH",
    "NEM","FCX","SCCO","RGLD","WPM","FNV","HMY","AEM","KGC","GFI",
    "ALB","SQM","LAC","PLL","ATI","CRS","X","CLF","STLD","NUE",
    # AI-related / momentum mid-caps frequently in scans
    "PLTR","AI","BBAI","SOUN","SERV","BTBT","RIOT","CLSK","MARA","CAN",
    "HUT","BITF","CIFR","GREE","WULF","IREN","CORZ","DGHI","BTDR","APLD",
    # Aerospace / defense / space
    "RKLB","ASTS","LUNR","RDW","PL","SPCE","JOBY","ARQQ","AVAV","RTX",
    # Misc large names that frequently appear
    "NFLX","DIS","CMCSA","T","VZ","TMUS","CHTR","DISH","SIRI","WBD",
    "ABBV","MRK","PFE","JNJ","BMY","LLY","TMO","ABT","MDT","SYK",
    "BA","CAT","DE","GE","HON","MMM","LMT","NOC","RTX","TXT",
}




# Rotate through these User-Agents when scraping Wikipedia / iShares — some
# sources block the default Python/requests UA but accept browser UAs.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]


# Module-level dict for source status, populated during scans.
# Format: {source_name: {"count": int, "ok": bool, "error": str or None, "fallback_used": bool}}
SOURCE_STATUS: dict = {}


def _record_source(name: str, count: int, ok: bool, error: Optional[str] = None,
                   fallback_used: bool = False) -> None:
    SOURCE_STATUS[name] = {
        "count": count, "ok": ok, "error": error, "fallback_used": fallback_used,
    }


def fetch_sp500_set() -> set[str]:
    """Try Wikipedia (with rotated User-Agents), fall back to hardcoded
    mega-caps if it fails. Records detailed outcome in SOURCE_STATUS."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    last_error = None
    for ua in _USER_AGENTS:
        try:
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=15)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(0.5)
                continue
            if "<table" not in resp.text.lower():
                last_error = f"No HTML table in response (got {len(resp.text)} chars)"
                time.sleep(0.5)
                continue
            tables = pd.read_html(StringIO(resp.text))
            live = set(tables[0]["Symbol"].tolist())
            if len(live) > 100:
                _record_source("S&P 500", len(live), True)
                return live
            last_error = f"Parsed table had only {len(live)} tickers"
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
        time.sleep(0.5)
    _record_source("S&P 500", len(_STATIC_SP500_FALLBACK), False,
                   error=last_error or "Unknown error",
                   fallback_used=True)
    return set(_STATIC_SP500_FALLBACK)


def fetch_nq100_set() -> set[str]:
    """Try Wikipedia (with rotated User-Agents), fall back to hardcoded Nasdaq 100 mega-caps."""
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    last_error = None
    for ua in _USER_AGENTS:
        try:
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=15)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(0.5)
                continue
            if "<table" not in resp.text.lower():
                last_error = f"No HTML table in response (got {len(resp.text)} chars)"
                time.sleep(0.5)
                continue
            tables = pd.read_html(StringIO(resp.text))
            for tbl in tables:
                cols = [str(c).strip() for c in tbl.columns]
                if "Ticker" in cols:
                    live = set(tbl["Ticker"].dropna().tolist())
                    if len(live) > 50:
                        _record_source("Nasdaq 100", len(live), True)
                        return live
            last_error = "No table with 'Ticker' column and >50 rows found"
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
        time.sleep(0.5)
    _record_source("Nasdaq 100", len(_STATIC_NQ100_FALLBACK), False,
                   error=last_error or "Unknown error",
                   fallback_used=True)
    return set(_STATIC_NQ100_FALLBACK)


# iShares CSVs label their Sector column with GICS names ("Information
# Technology"), but the rest of the scanner uses yfinance's vocabulary
# ("Technology"). Normalize on the way in so downstream code only ever sees
# one set of sector names. Maps everything to a key that's also present in
# SECTOR_ABBREV so the short-form display still resolves.
_ISHARES_TO_YF_SECTOR = {
    "Information Technology": "Technology",
    "Health Care": "Healthcare",
    "Financials": "Financial Services",
    "Consumer Discretionary": "Consumer Cyclical",
    "Communication": "Communication Services",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Consumer Staples": "Consumer Defensive",
    "Energy": "Energy",
    "Real Estate": "Real Estate",
    "Materials": "Basic Materials",
    "Utilities": "Utilities",
}


def _fetch_ishares_holdings(url: str, with_sectors: bool = False):
    """Generic iShares ETF holdings CSV parser with UA rotation.

    Returns:
      - (ticker_set, error_message_if_any) when ``with_sectors=False`` (default)
      - (ticker_set, sector_map, error_message_if_any) when ``with_sectors=True``
        where ``sector_map`` is ``{ticker: yfinance_sector_name}`` for every
        ticker whose CSV row included a recognizable Sector column. Sector
        names are normalized to yfinance vocabulary so the cache stays
        consistent regardless of source.

    iShares CSV format (as of mid-2026):
      - First N lines: fund metadata (name, dates, etc.)
      - Then a header row with column names like:
          Ticker, Name, Sector, Asset Class, Market Value, Weight (%), ...
        — sometimes "Ticker" is renamed to "Symbol" or "Issuer Ticker"
      - Then data rows
    Our parser:
      1. Find the first line that LOOKS like a CSV header — comma-separated,
         contains one of {Ticker, Symbol, Issuer Ticker} as an EXACT token
         (not just a substring, so a metadata line mentioning the word
         "ticker" doesn't get misread as the header)
      2. Accept any of those column names after pandas parses
    """
    accepted_ticker_cols = {"ticker", "symbol", "issuer ticker", "ticker symbol"}
    last_error = None

    for ua in _USER_AGENTS:
        try:
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=30)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(0.5)
                continue

            lines = resp.text.split("\n")

            # Find a line that looks like a CSV header — must be comma-separated
            # and contain one of our accepted ticker column names AS A TOKEN.
            header_idx = None
            for i, ln in enumerate(lines):
                if len(ln) > 500 or "," not in ln:
                    continue
                # Tokenize the line, strip quotes/whitespace from each token
                tokens = [t.strip().strip('"').lower() for t in ln.split(",")]
                if any(tok in accepted_ticker_cols for tok in tokens):
                    header_idx = i
                    break

            if header_idx is None:
                preview = resp.text[:200].replace("\n", " ")
                last_error = f"No CSV header row found with Ticker/Symbol column (got: {preview!r}...)"
                time.sleep(0.5)
                continue

            df_etf = pd.read_csv(
                StringIO("\n".join(lines[header_idx:])), on_bad_lines="skip"
            )
            ticker_col = next(
                (c for c in df_etf.columns
                 if c.strip().lower() in accepted_ticker_cols),
                None,
            )
            if ticker_col is None:
                cols_found = ", ".join(repr(c) for c in df_etf.columns[:8])
                last_error = f"No Ticker/Symbol column after parse (cols: {cols_found})"
                time.sleep(0.5)
                continue

            # Sector column is optional — present in IWV/IWM/IVV, absent in some
            # bond / commodity funds. Look it up by case-insensitive name match.
            sector_col = next(
                (c for c in df_etf.columns if c.strip().lower() == "sector"),
                None,
            )

            out: set[str] = set()
            sector_map: dict[str, str] = {}
            for _, row in df_etf.iterrows():
                t = row.get(ticker_col)
                if pd.isna(t):
                    continue
                t_clean = str(t).strip().lstrip("$")
                if not (
                    t_clean
                    and t_clean not in ("-", "nan", "CASH", "USD", "")
                    and len(t_clean) <= 6
                    and t_clean.replace("-", "").isalpha()
                ):
                    continue
                out.add(t_clean)
                if sector_col is not None:
                    raw_sec = row.get(sector_col)
                    if pd.notna(raw_sec):
                        sec_str = str(raw_sec).strip()
                        # Normalize to yfinance vocabulary so the cache stays
                        # consistent. If we don't recognize it, drop it rather
                        # than poisoning the cache with a mismatched label.
                        norm = _ISHARES_TO_YF_SECTOR.get(sec_str)
                        if norm:
                            sector_map[t_clean] = norm
            if out:
                if with_sectors:
                    return out, sector_map, None
                return out, None
            last_error = "Parsed 0 valid tickers"
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
        time.sleep(0.5)
    if with_sectors:
        return set(), {}, last_error
    return set(), last_error


def prime_sector_from_ishares(progress: ProgressCallback = None) -> int:
    """Fetch iShares IWV (Russell 3000) holdings to prime the fundamentals
    cache with sector data for ~3,000 tickers, no yfinance calls required.

    Does NOT provide industry data — iShares only ships GICS sector
    granularity, and the rotation page wants industry. This is belt-and-
    suspenders: ensures EVERY widely-traded US stock has at least a sector
    label cached, so when fetch_industry_only fails or hasn't run yet,
    the sector field is still populated and downstream code degrades
    gracefully (e.g. sector-level rotation fallback).

    Cheap — one HTTP request. Stamps the entries as "static" cached so they
    participate in the 90-day TTL like any yfinance-sourced sector. If a
    ticker already has BOTH sector and industry cached, this function leaves
    it alone (don't overwrite a complete entry with a partial one).

    Returns: count of cache entries updated with sector data.
    """
    from scanner.data_cache import (
        load_fundamentals_cache, save_fundamentals_cache,
        get_cached_fundamentals, update_fundamentals_entry,
    )

    if progress:
        progress("ishares_prime", 0, 1, "Priming sector cache from iShares IWV…")

    out, sector_map, err = _fetch_ishares_holdings(
        "https://www.ishares.com/us/products/239714/"
        "ishares-russell-3000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWV_holdings&dataType=fund",
        with_sectors=True,
    )

    if not sector_map:
        if progress:
            progress("ishares_prime", 1, 1,
                     f"iShares sector priming skipped ({err or 'no sector data'})")
        return 0

    cache = load_fundamentals_cache()
    n_updated = 0
    for ticker, yf_sector in sector_map.items():
        entry = get_cached_fundamentals(ticker, cache)
        existing_sector = (entry.get("sector") or "").strip()
        existing_industry = (entry.get("industry") or "").strip()
        # Skip if we already have BOTH fields populated — don't downgrade.
        if existing_sector and existing_sector != "N/A" and existing_industry and existing_industry != "N/A":
            continue
        # Otherwise stamp in the sector. Industry stays unset — fetch_industry_only
        # will fill it from yfinance later.
        sector_short = SECTOR_ABBREV.get(yf_sector, yf_sector)
        static_data = {
            "sector": yf_sector,
            "sector_short": sector_short,
        }
        # Preserve any existing industry value if present
        if existing_industry and existing_industry != "N/A":
            static_data["industry"] = existing_industry
        update_fundamentals_entry(cache, ticker, static=static_data)
        n_updated += 1

    save_fundamentals_cache(cache)
    if progress:
        progress("ishares_prime", 1, 1,
                 f"iShares sector priming: +{n_updated} entries updated")
    return n_updated


def fetch_nasdaq_trader_set() -> set[str]:
    """Official NASDAQ-published lists of all US-listed common stocks.

    Two pipe-delimited files updated daily by NASDAQ Trader:
      - nasdaqlisted.txt: NASDAQ-listed stocks (~4,000)
      - otherlisted.txt: NYSE / AMEX / CBOE-listed stocks (~5,000)

    Combined gives ~8,000 US common-stock tickers. Public, no auth, no
    rate-limiting. The most reliable single source — this is exactly what
    professional data vendors use as their seed list.

    Filters out: ETFs, test issues, preferred shares (anything with `.`, `$`,
    `/`, `=`, `^` in the symbol).
    """
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]
    all_tickers: set[str] = set()
    errors: list[str] = []

    for url in urls:
        fname = url.split("/")[-1]
        try:
            resp = requests.get(url, headers={"User-Agent": _USER_AGENTS[0]}, timeout=30)
            if resp.status_code != 200:
                errors.append(f"{fname}: HTTP {resp.status_code}")
                continue
            lines = resp.text.splitlines()
            if len(lines) < 5:
                errors.append(f"{fname}: too few lines ({len(lines)})")
                continue
            header = [h.strip().lower() for h in lines[0].split("|")]
            # Find symbol column — different files use different header names
            sym_idx = next(
                (i for i, h in enumerate(header) if h in ("symbol", "act symbol")),
                None,
            )
            if sym_idx is None:
                errors.append(f"{fname}: no symbol column in header: {header[:5]}")
                continue
            etf_idx = next((i for i, h in enumerate(header) if h == "etf"), None)
            test_idx = next((i for i, h in enumerate(header) if h == "test issue"), None)

            for line in lines[1:]:
                # Last line is "File Creation Time: ..." — skip non-data rows
                if not line or "|" not in line or line.startswith("File Creation"):
                    continue
                fields = line.split("|")
                if len(fields) <= sym_idx:
                    continue
                # Skip ETFs and test issues
                if etf_idx is not None and len(fields) > etf_idx and fields[etf_idx].strip().upper() == "Y":
                    continue
                if test_idx is not None and len(fields) > test_idx and fields[test_idx].strip().upper() == "Y":
                    continue
                sym = fields[sym_idx].strip().upper()
                if not sym or sym == "SYMBOL":
                    continue
                # Filter symbols with special characters (preferred shares, units,
                # warrants, etc.) — keep only clean common stock tickers
                if any(c in sym for c in (".", "$", "/", "=", "^", "+", "*", " ")):
                    continue
                if len(sym) > 6 or len(sym) < 1:
                    continue
                if not sym.replace("-", "").isalpha():
                    continue
                all_tickers.add(sym)
        except Exception as e:
            errors.append(f"{fname}: {type(e).__name__}: {str(e)[:100]}")

    if all_tickers and len(all_tickers) > 1000:
        _record_source("NASDAQ Trader (all US-listed)", len(all_tickers), True)
        return all_tickers

    error_msg = " | ".join(errors) if errors else f"Only {len(all_tickers)} tickers"
    _record_source(
        "NASDAQ Trader (all US-listed)",
        len(all_tickers),
        False,
        error=error_msg[:200],
        fallback_used=True,
    )
    return all_tickers


def fetch_iwm_set() -> set[str]:
    """Russell 2000 via IWM. Falls back to curated mid-cap list if iShares unreachable."""
    out, err = _fetch_ishares_holdings(
        "https://www.ishares.com/us/products/239710/"
        "ISHARES-RUSSELL-2000-ETF/1467271812596.ajax"
        "?fileType=csv&fileName=IWM_holdings&dataType=fund"
    )
    if len(out) > 100:
        _record_source("Russell 2000 (IWM)", len(out), True)
        return out
    merged = out | _STATIC_RUT_FALLBACK
    _record_source("Russell 2000 (IWM)", len(merged), False,
                   error=err or "Empty / too few tickers returned",
                   fallback_used=True)
    return merged


def fetch_iwv_set() -> set[str]:
    """Russell 3000 via IWV. Falls back to curated mid-cap list if iShares unreachable."""
    out, err = _fetch_ishares_holdings(
        "https://www.ishares.com/us/products/239714/"
        "ishares-russell-3000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWV_holdings&dataType=fund"
    )
    if len(out) > 500:
        _record_source("Russell 3000 (IWV)", len(out), True)
        return out
    merged = out | _STATIC_RUT_FALLBACK
    _record_source("Russell 3000 (IWV)", len(merged), False,
                   error=err or "Empty / too few tickers returned",
                   fallback_used=True)
    return merged


def collect_tickers(progress: ProgressCallback = None) -> tuple[list[str], set[str], set[str]]:
    """Returns (cleaned_tickers, sp500_set, nq100_set).
    Universe sources, in priority order:
      1. NASDAQ Trader (all US-listed common stocks ~8,000) — PRIMARY
      2. S&P 500 + Nasdaq 100 (Wikipedia) — for INDEX column labels
      3. iShares IWM + IWV (Russell 2000/3000) — backup when NASDAQ Trader fails
    The sp500_set and nq100_set returned are used purely for index labeling.
    """
    if progress:
        progress("tickers", 0, 5, "Loading NASDAQ Trader (all US-listed)…")
    nasdaq_trader = fetch_nasdaq_trader_set()

    if progress:
        progress("tickers", 1, 5, "Loading S&P 500…")
    sp500 = fetch_sp500_set()
    if progress:
        progress("tickers", 2, 5, "Loading Nasdaq-100…")
    nq100 = fetch_nq100_set()

    # Only hit iShares if NASDAQ Trader fell short — they're our backup, not primary
    if len(nasdaq_trader) < 1000:
        if progress:
            progress("tickers", 3, 5, "Loading Russell 2000 (IWM)…")
        iwm = fetch_iwm_set()
        if progress:
            progress("tickers", 4, 5, "Loading Russell 3000 (IWV)…")
        iwv = fetch_iwv_set()
    else:
        # NASDAQ Trader already covers everything iShares would give us
        iwm = set()
        iwv = set()
        # Mark them as "skipped — NASDAQ Trader covered it"
        _record_source("Russell 2000 (IWM)", 0, True,
                       error="Skipped — NASDAQ Trader already provides full coverage")
        _record_source("Russell 3000 (IWV)", 0, True,
                       error="Skipped — NASDAQ Trader already provides full coverage")

    if progress:
        progress("tickers", 5, 5, "Combining and deduplicating…")

    raw = set()
    raw.update(nasdaq_trader); raw.update(sp500); raw.update(nq100)
    raw.update(iwm); raw.update(iwv)

    cleaned: list[str] = []
    seen: set[str] = set()
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().lstrip("$").upper().replace(".", "-")
        if t in ("", "-", "NAN", "CASH", "USD") or t in seen:
            continue
        if len(t) < 1 or len(t) > 6:
            continue
        parts = t.split("-")
        if not all(p.isalpha() for p in parts if p):
            continue
        seen.add(t)
        cleaned.append(t)

    return cleaned, sp500, nq100


def _batch_yf_download(
    tickers: list[str],
    start_date,
    end_date=None,
    batch_size: int = 50,
) -> dict[str, pd.DataFrame]:
    """Download price history in batches via yf.download(...).

    yf.download accepts a list of tickers and returns a single MultiIndex
    DataFrame. Internally it uses a thread pool, so a batch of 50 typically
    runs 2-3x faster than 50 serial Ticker.history() calls. Returns a
    dict {ticker: DataFrame}; tickers that yfinance couldn\'t fetch are
    simply absent from the output (caller treats that as a cache miss).
    """
    out: dict[str, pd.DataFrame] = {}
    if not tickers:
        return out

    start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
    end_str = end_date.strftime("%Y-%m-%d") if (end_date is not None and hasattr(end_date, "strftime")) else None

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            df = yf.download(
                tickers=batch,
                start=start_str,
                end=end_str,
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=True,
            )
        except Exception:
            # If the whole batch fails, retry one ticker at a time so a
            # single bad symbol doesn\'t poison the rest. Slow path.
            for t in batch:
                try:
                    d = yf.Ticker(t).history(start=start_str, end=end_str, interval="1d")
                    if isinstance(d.columns, pd.MultiIndex):
                        d.columns = d.columns.get_level_values(0)
                    if not d.empty:
                        out[t] = d
                except Exception:
                    continue
            continue

        # Parse the MultiIndex DataFrame back into per-ticker DataFrames.
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            # group_by="ticker" → top-level keys are tickers
            level0 = df.columns.get_level_values(0).unique()
            for t in batch:
                if t in level0:
                    try:
                        sub = df[t].dropna(how="all")
                        if not sub.empty:
                            out[t] = sub
                    except Exception:
                        continue
        else:
            # Single-ticker batch — no MultiIndex
            if len(batch) == 1:
                try:
                    sub = df.dropna(how="all")
                    if not sub.empty:
                        out[batch[0]] = sub
                except Exception:
                    pass
    return out


def download_history_and_rs(
    tickers: list[str],
    settings: ScanSettings,
    progress: ProgressCallback = None,
) -> tuple[dict, pd.DataFrame, dict, int, int]:
    """Download or incrementally refresh price history, then compute RS.

    Pipeline:
      1. Load each ticker\'s cached price DataFrame from disk (instant).
      2. Partition tickers into:
           * cold      — no cache; need a full 540-day download
           * incremental — cached but missing recent days; need a small fetch
           * fresh     — cache already includes the latest session
      3. Batch-download cold + incremental groups via yf.download.
      4. For incrementals, detect splits by comparing overlapping closes;
         if a split is suspected, re-download fully for that ticker.
      5. Apply liquidity filter, compute RS rankings.

    All cache misses + failures fall back to a fresh download. Safety net:
    any per-ticker exception is caught and that ticker is skipped, never
    crashes the whole scan.
    """
    from scanner.data_cache import (
        load_price_history, save_price_history,
        looks_like_split, merge_price_history,
    )

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=540)
    total = len(tickers)

    if progress:
        progress("history", 0, total, f"Reading price cache for {total} tickers...")

    today_date = pd.Timestamp.now().normalize().date()
    cached_dfs: dict[str, pd.DataFrame] = {}
    needs_full: list[str] = []
    needs_incremental: dict[str, pd.DataFrame] = {}

    for ticker in tickers:
        cached = load_price_history(ticker)
        if cached is None or cached.empty:
            needs_full.append(ticker)
            continue
        try:
            last_date = cached.index.max().date() if hasattr(cached.index.max(), "date") else None
        except Exception:
            last_date = None
        if last_date is None:
            needs_full.append(ticker)
            continue
        # Refresh anything more than 0 days stale (market closed days
        # tolerate this — the incremental fetch will just return empty)
        if last_date >= today_date:
            cached_dfs[ticker] = cached
        else:
            needs_incremental[ticker] = cached

    n_fresh = len(cached_dfs)
    n_full = len(needs_full)
    n_incr = len(needs_incremental)

    # --- Cold-cache full download (batched) ---
    if needs_full:
        if progress:
            progress("history", n_fresh, total,
                     f"Full download for {n_full} tickers (cold cache)...")
        full_dfs = _batch_yf_download(needs_full, start_dt, end_dt, batch_size=50)
        for t, df in full_dfs.items():
            cached_dfs[t] = df
            save_price_history(t, df)

    # --- Incremental refresh: fetch last ~10 days, merge, detect splits ---
    if needs_incremental:
        if progress:
            progress("history", n_fresh + n_full, total,
                     f"Incremental refresh for {n_incr} tickers (warm cache)...")
        incr_start = end_dt - timedelta(days=10)
        fresh_dfs = _batch_yf_download(
            list(needs_incremental.keys()), incr_start, end_dt, batch_size=50,
        )
        full_redownloads: list[str] = []
        for t, fresh_df in fresh_dfs.items():
            cached = needs_incremental[t]
            if looks_like_split(cached, fresh_df):
                # Probable split — schedule a full re-download
                full_redownloads.append(t)
            else:
                merged = merge_price_history(cached, fresh_df)
                cached_dfs[t] = merged
                save_price_history(t, merged)
        # Tickers that yfinance didn\'t return for incremental — keep stale cache
        for t, cached in needs_incremental.items():
            if t not in cached_dfs and t not in full_redownloads:
                cached_dfs[t] = cached

        if full_redownloads:
            if progress:
                progress("history", n_fresh + n_full, total,
                         f"Split detected — re-downloading {len(full_redownloads)} tickers in full...")
            redo_dfs = _batch_yf_download(full_redownloads, start_dt, end_dt, batch_size=50)
            for t, df in redo_dfs.items():
                cached_dfs[t] = df
                save_price_history(t, df)

    # --- Apply liquidity filter + RS computation on assembled cache ---
    stock_cache: dict[str, pd.DataFrame] = {}
    rs_list: list[dict] = []
    skipped_liquidity = 0
    skipped_history = 0

    if progress:
        progress("history", total, total,
                 f"Computing RS for {len(cached_dfs)} tickers (cache: {n_fresh} fresh, {n_incr} warm, {n_full} cold)...")

    for ticker, raw_df in cached_dfs.items():
        try:
            df = clean_history(raw_df)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
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
            c = df["Close"].iloc[-1]
            c_63 = df["Close"].iloc[-63]
            c_126 = df["Close"].iloc[-126]
            c_189 = df["Close"].iloc[-189]
            c_252 = df["Close"].iloc[-252]
            if c_63 == 0:
                continue
            score = (
                0.4 * ((c - c_63) / c_63)
                + 0.2 * ((c - c_126) / c_126)
                + 0.2 * ((c - c_189) / c_189)
                + 0.2 * ((c - c_252) / c_252)
            )
            rs_list.append({"Ticker": ticker, "Raw_Score": score})
        except Exception:
            continue

    rs_df = pd.DataFrame(rs_list).dropna(subset=["Raw_Score"]) if rs_list else pd.DataFrame(columns=["Ticker", "Raw_Score"])
    if len(rs_df):
        rs_df["Rank_Pct"] = rs_df["Raw_Score"].rank(pct=True)
        rs_df["RS_Rating"] = (rs_df["Rank_Pct"] * 99).fillna(0).astype(int)
        rs_map = rs_df.set_index("Ticker")["RS_Rating"].to_dict()
    else:
        rs_map = {}

    return stock_cache, rs_df, rs_map, skipped_history, skipped_liquidity


def calc_breadth(stock_cache: dict, sp_trend: str, nq_trend: str, rut_trend: str = "N/A"):
    """Compute market-internals snapshot.

    Changes vs the original loose version:
      - NEW HIGHS / NEW LOWS use a STRICT definition: today's close must
        actually exceed (or undercut) every close in the prior 251 sessions.
        No ±1% tolerance. This is the textbook NYSE definition and is
        immune to rolling-window anniversary noise that can flip the count
        wildly day-to-day in the loose version.
      - Tighter GREEN thresholds (U/D ≥ 1.3, pct_above_50 ≥ 60, NH ≥ 1.5×NL)
        so a barely-positive day does not get labelled "Accumulation".
      - Caps breadth at YELLOW when Russell 2000 is DOWN while S&P/Nasdaq
        are UP — this is the small-caps-lagging divergence; treating it as
        GREEN would contradict the market-signal banner.
      - NARROWING detector: compares today's pct_above_50 to the same
        figure 10 trading sessions ago. A drop of ≥5 percentage points
        flags narrowing participation even when single-day metrics still
        look fine.
    """
    total = len(stock_cache)
    above_50 = above_200 = up_vol = down_vol = new_highs = new_lows = 0
    # rolling-window value 10 sessions ago for the narrowing detector
    above_50_10d_ago = 0
    counted_10d_ago = 0

    for df in stock_cache.values():
        try:
            curr = df["Close"].iloc[-1]
            prev = df["Close"].iloc[-2]
            vol = df["Volume"].iloc[-1]

            # STRICT new high / new low: compare today vs prior 251 sessions
            # (i.e. exclude today from the lookback window).
            if len(df) >= 252:
                prior_window = df["Close"].iloc[-252:-1]
            else:
                prior_window = df["Close"].iloc[:-1]
            prior_high = prior_window.max() if len(prior_window) else curr
            prior_low = prior_window.min() if len(prior_window) else curr

            sma_50_series = df["Close"].rolling(50).mean()
            sma_50 = sma_50_series.iloc[-1]
            sma_200 = df["Close"].rolling(200).mean().iloc[-1]

            if curr > sma_50:
                above_50 += 1
            if curr > sma_200:
                above_200 += 1
            if curr > prior_high:
                new_highs += 1
            elif curr < prior_low:
                new_lows += 1
            if curr > prev:
                up_vol += vol
            elif curr < prev:
                down_vol += vol

            # Narrowing detector: same stock's position vs its 50-day MA
            # 10 trading sessions ago. Needs at least 60 bars (50 for the
            # MA + 10 for the look-back).
            if len(df) >= 60:
                close_10d_ago = df["Close"].iloc[-11]
                sma_50_10d_ago = sma_50_series.iloc[-11]
                if pd.notna(sma_50_10d_ago):
                    if close_10d_ago > sma_50_10d_ago:
                        above_50_10d_ago += 1
                    counted_10d_ago += 1
        except Exception:
            continue

    pct_above_50 = (above_50 / total) * 100 if total else 0.0
    pct_above_200 = (above_200 / total) * 100 if total else 0.0
    up_down_ratio = (up_vol / down_vol) if down_vol > 0 else 99.0
    pct_above_50_10d_ago = (
        (above_50_10d_ago / counted_10d_ago) * 100 if counted_10d_ago else pct_above_50
    )
    # NARROWING = participation has dropped meaningfully over the last two weeks
    narrowing = (pct_above_50_10d_ago - pct_above_50) >= 5.0

    # ------ Status decision tree ------
    # Worst-first; first match wins.
    # Trend labels are ">50MA" / "<50MA" (renamed from "UP"/"DOWN" 2026-07-22).
    # Meaning unchanged: "<50MA" = index closed below its 50-day MA = trend broken.
    trend_broken = sp_trend == "<50MA" or nq_trend == "<50MA"
    small_cap_divergence = (
        sp_trend == ">50MA" and nq_trend == ">50MA" and rut_trend == "<50MA"
    )
    # Tightened GREEN gates
    strong_ud = up_down_ratio >= 1.3
    strong_part = pct_above_50 >= 60.0
    nh_dominant = new_highs >= 1.5 * max(new_lows, 1)

    if new_lows > new_highs:
        breadth_status = "RED (New Lows Dominating)"
    elif up_down_ratio < 1.0 and pct_above_50 < 40:
        breadth_status = "RED (Broad Weakness)"
    elif trend_broken:
        # Major index broken — never GREEN even if internals look OK
        breadth_status = "YELLOW (Speculative Bounce - Primary Trend Down)"
    elif small_cap_divergence:
        breadth_status = "YELLOW (Narrowing - Small Caps Lagging)"
    elif narrowing:
        breadth_status = "YELLOW (Narrowing - Participation Falling)"
    elif strong_ud and strong_part and nh_dominant:
        breadth_status = "GREEN (Accumulation - Broad Participation)"
    else:
        breadth_status = "YELLOW (Mixed Internals)"

    return (
        pct_above_50, pct_above_200, up_down_ratio,
        new_highs, new_lows, breadth_status,
        pct_above_50_10d_ago, narrowing,
    )


def _next_earnings_date(t_obj) -> Optional[str]:
    """Best-effort next earnings date (YYYY-MM-DD) from yfinance."""
    try:
        cal = t_obj.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            val = cal.get("Earnings Date")
            if isinstance(val, list) and val:
                val = val[0]
            return str(val)[:10] if val else None
        if hasattr(cal, "loc") and "Earnings Date" in cal.index:
            val = cal.loc["Earnings Date"].iloc[0]
            return str(val)[:10] if val else None
    except Exception:
        pass
    return None


# Threading + rate-limit globals for the fundamentals fetcher.
# These are mutated by the backoff helper so a single 429 storm doesn\'t
# blow up the whole scan — we just slow down for the rest of this run.
_FUND_MAX_WORKERS = 12      # starts at 12, dynamically reduced on 429s (bumped from 8)
_FUND_RATE_LOCK = None      # lazily allocated on first use


def _compute_growth_flags(quarterly_financials: pd.DataFrame) -> tuple[bool, bool, bool]:
    """Parse quarterly_financials into (eps_flag, eps_accel, rev_flag).

    Resilient to yfinance schema changes that have happened across versions:
      - Net Income row has been called: "Net Income", "NetIncome",
        "Net Income Common Stockholders", "NetIncomeCommonStockholders"
      - Total Revenue row has been called: "Total Revenue", "TotalRevenue",
        "OperatingRevenue", "Revenue"
      - Number of returned quarters has varied (sometimes 4, sometimes 5-8)

    Strategy: try each known row-name in order until one is found, then do
    YoY comparison using whatever quarters are available. If there are at
    least 5 quarters use iloc[0] vs iloc[4] (true YoY). If fewer, compare
    the most recent quarter to the oldest available (still directional, just
    not perfectly YoY) — better than always returning False.
    """
    eps_flag = eps_accel = rev_flag = False

    def _find_row(df: pd.DataFrame, candidates: tuple[str, ...]) -> Optional[pd.Series]:
        try:
            idx = df.index
            for name in candidates:
                if name in idx:
                    return df.loc[name]
            # Case-insensitive fallback: handle Yahoo capitalisation drift
            lowmap = {str(k).lower().replace(" ", ""): k for k in idx}
            for name in candidates:
                key = name.lower().replace(" ", "")
                if key in lowmap:
                    return df.loc[lowmap[key]]
        except Exception:
            pass
        return None

    _NI_NAMES = (
        "Net Income",
        "NetIncome",
        "Net Income Common Stockholders",
        "NetIncomeCommonStockholders",
        "Net Income Continuous Operations",
        "NetIncomeContinuousOperations",
        "Net Income From Continuing Operation Net Minority Interest",
        "NetIncomeFromContinuingOperationNetMinorityInterest",
        "Net Income From Continuing And Discontinued Operation",
        "NetIncomeFromContinuingAndDiscontinuedOperation",
        "Net Income Including Noncontrolling Interests",
        "NetIncomeIncludingNoncontrollingInterests",
        "Diluted NI Availto Com Stockholders",
        "DilutedNIAvailtoComStockholders",
        "Normalized Income",
        "NormalizedIncome",
    )
    _REV_NAMES = (
        "Total Revenue",
        "TotalRevenue",
        "Operating Revenue",
        "OperatingRevenue",
        "Revenue",
        "Net Sales",
        "NetSales",
    )

    try:
        if quarterly_financials is None or quarterly_financials.empty:
            return (False, False, False)

        ni = _find_row(quarterly_financials, _NI_NAMES)
        if ni is not None:
            ni = ni.dropna()
            if len(ni) >= 5 and ni.iloc[4] != 0:
                # Preferred path: true YoY (Q0 vs Q4)
                growth_q0 = (ni.iloc[0] - ni.iloc[4]) / abs(ni.iloc[4])
                if growth_q0 > 0.25:
                    eps_flag = True
                    if len(ni) >= 6 and ni.iloc[5] != 0:
                        growth_q1 = (ni.iloc[1] - ni.iloc[5]) / abs(ni.iloc[5])
                        if growth_q0 > growth_q1:
                            eps_accel = True
            elif len(ni) >= 2 and ni.iloc[-1] != 0:
                # Fallback: yfinance returned <5 quarters. Compare newest to
                # oldest available — directional signal only, no acceleration.
                growth = (ni.iloc[0] - ni.iloc[-1]) / abs(ni.iloc[-1])
                if growth > 0.25:
                    eps_flag = True

        rev = _find_row(quarterly_financials, _REV_NAMES)
        if rev is not None:
            rev = rev.dropna()
            if len(rev) >= 5 and rev.iloc[4] != 0:
                if ((rev.iloc[0] - rev.iloc[4]) / abs(rev.iloc[4])) > 0.20:
                    rev_flag = True
            elif len(rev) >= 2 and rev.iloc[-1] != 0:
                if ((rev.iloc[0] - rev.iloc[-1]) / abs(rev.iloc[-1])) > 0.20:
                    rev_flag = True
    except Exception:
        pass
    return eps_flag, eps_accel, rev_flag


def _is_rate_limit_error(exc: Exception) -> bool:
    """Best-effort check whether a yfinance exception is a rate-limit."""
    try:
        s = str(exc).lower()
    except Exception:
        return False
    return any(tok in s for tok in ("429", "rate limit", "too many requests", "rate-limit"))


def _fetch_one_fundamentals(
    ticker: str,
    need_static: bool,
    need_mktcap: bool,
    need_growth: bool,
    max_retries: int = 5,
):
    """Fetch the fundamentals fields that are stale for one ticker.

    Returns (static_data, mktcap_data, growth_data) tuple. Each is either
    a dict of fields or None (when that group didn't need refreshing or
    the fetch failed).

    Exponential backoff on 429 / rate-limit errors. After max_retries the
    function returns whatever it managed (possibly None for the failed
    groups). Never raises — the caller can fall back to the cache.
    """
    backoff = 1.0
    static_data = mktcap_data = growth_data = None

    for attempt in range(max_retries):
        try:
            t_obj = yf.Ticker(ticker)

            # `info` covers static + mktcap + PEG (a single network call)
            if need_static or need_mktcap or need_growth:
                info = safe_get_info(t_obj) or {}
                if need_static:
                    sector = info.get("sector", "N/A") or "N/A"
                    industry = info.get("industry", "N/A") or "N/A"
                    sector_short = SECTOR_ABBREV.get(sector, sector)
                    static_data = {
                        "sector": sector,
                        "industry": industry,
                        "sector_short": sector_short,
                    }
                if need_mktcap:
                    held_pct = info.get("heldPercentInstitutions")
                    try:
                        held_pct = round(float(held_pct) * 100, 1) if held_pct is not None else None
                    except Exception:
                        held_pct = None
                    mktcap_data = {
                        "mktcap_raw": info.get("marketCap"),
                        "mktcap_display": format_mktcap(info.get("marketCap")),
                        "shares_outstanding": info.get("sharesOutstanding"),
                        "held_pct_institutions": held_pct,
                    }
                if need_growth:
                    peg_raw = info.get("pegRatio")
                    try:
                        peg = round(float(peg_raw), 2) if peg_raw is not None else None
                    except Exception:
                        peg = None
                    growth_data = {"peg": peg}

            if need_growth:
                fin = t_obj.quarterly_financials
                eps_flag, eps_accel, rev_flag = _compute_growth_flags(fin)
                notes = []
                if eps_flag:
                    notes.append("EPS++" if eps_accel else "EPS+")
                if rev_flag:
                    notes.append("Sale+")
                fund_display = " ".join(notes) if notes else "Lag"
                growth_data = (growth_data or {})
                growth_data.update({
                    "eps_flag": eps_flag,
                    "eps_accel": eps_accel,
                    "rev_flag": rev_flag,
                    "fund_display": fund_display,
                    "earnings_date": _next_earnings_date(t_obj),
                })

            return (static_data, mktcap_data, growth_data)
        except Exception as exc:
            # Treat ANY exception as potentially rate-limit-related and retry
            # with exponential backoff. Yahoo's throttle responses don't always
            # look like classic 429s — sometimes JSON parse errors, sometimes
            # HTML error pages that yfinance can't handle. Better to retry than
            # to bail at the first hiccup.
            if attempt < max_retries - 1:
                global _FUND_MAX_WORKERS
                if _is_rate_limit_error(exc) and _FUND_MAX_WORKERS > 2:
                    _FUND_MAX_WORKERS = max(2, _FUND_MAX_WORKERS - 1)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 32.0)
                continue
            return (static_data, mktcap_data, growth_data)
    return (static_data, mktcap_data, growth_data)



def fetch_industry_only(
    tickers,
    progress: ProgressCallback = None,
    max_new: Optional[int] = None,
) -> tuple[int, int]:
    """Backfill industry/sector for tickers missing those fields from the cache.

    Cheaper sibling of ``fetch_fundamentals_sector_mktcap``:
      - One yfinance .info call per ticker (no quarterly_financials, no calendar)
      - Skips PEG, EPS flags, mktcap, ownership, earnings_date
      - Writes only the "static" group (sector, industry, sector_short)

    This is what makes the Rotation page show the FULL RS distribution
    instead of being skewed to RS>=70. The heavy fundamentals fetch below
    is gated by an RS pre-filter, so low-RS tickers never get an industry
    assignment from THAT path. This function fills the gap by running on
    the FULL ``rs_map``, not just the high-RS subset.

    Industry classifications change very rarely (90-day TTL is a formality),
    so once a ticker is cached, it stays cached forever in practice. Only
    the first scan after this rolls out pays the full cost — every scan
    after that is near-zero added work.

    Args:
        tickers: iterable of ticker symbols to backfill industry for
        progress: optional callback (phase, done, total, message)
        max_new: budget cap on first-scan cost. None = unlimited (recommended
                 — the cache is permanent, so paying once is better than
                 paying a partial bill every scan).

    Returns: (n_new_fetched, n_failed)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scanner.data_cache import (
        load_fundamentals_cache, save_fundamentals_cache,
        get_cached_fundamentals, update_fundamentals_entry,
    )

    global _FUND_MAX_WORKERS
    workers = _FUND_MAX_WORKERS  # piggy-back on the dynamic worker pool

    cache = load_fundamentals_cache()

    # Identify which tickers need an industry fetch. We treat the entry as
    # complete iff it has a non-"N/A" industry field. Stale-but-present
    # entries are left alone — industry never changes in practice and we'd
    # rather spend the budget on tickers with zero coverage.
    missing: list[str] = []
    for t in tickers:
        entry = get_cached_fundamentals(t, cache)
        ind = (entry.get("industry") or "").strip()
        if not ind or ind == "N/A":
            missing.append(t)

    if max_new is not None and len(missing) > max_new:
        missing = missing[:max_new]  # budget cap — process the rest next scan

    if not missing:
        if progress:
            progress("industry", 1, 1, "Industry universe fully cached — skipping backfill")
        return 0, 0

    if progress:
        progress(
            "industry", 0, len(missing),
            f"Backfilling industry/sector for {len(missing)} tickers (one-time cost)…",
        )

    def _one(ticker: str):
        backoff = 1.0
        for attempt in range(4):
            try:
                t_obj = yf.Ticker(ticker)
                info = safe_get_info(t_obj) or {}
                sector = info.get("sector", "N/A") or "N/A"
                industry = info.get("industry", "N/A") or "N/A"
                sector_short = SECTOR_ABBREV.get(sector, sector)
                # Only treat as success if we got AT LEAST an industry — a
                # blank info dict (delisted ticker, etc.) shouldn't poison
                # the cache with "N/A" entries we'd never refresh.
                if industry == "N/A" and sector == "N/A":
                    return ticker, None
                return ticker, {
                    "sector": sector,
                    "industry": industry,
                    "sector_short": sector_short,
                }
            except Exception as exc:
                if _is_rate_limit_error(exc) and attempt < 3:
                    global _FUND_MAX_WORKERS
                    if _FUND_MAX_WORKERS > 2:
                        _FUND_MAX_WORKERS = max(2, _FUND_MAX_WORKERS - 1)
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, 32.0)
                    continue
                break
        return ticker, None

    n_new = 0
    n_fail = 0
    done = 0
    # Persist to disk every N completions so a Ctrl+C halfway through
    # the backfill doesn't lose all the progress.
    save_interval = 250

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_one, t): t for t in missing}
        for fut in as_completed(futures):
            ticker, static_data = fut.result()
            done += 1
            if static_data is not None:
                update_fundamentals_entry(cache, ticker, static=static_data)
                n_new += 1
            else:
                n_fail += 1
            # Periodic save so we don't lose progress on a long backfill
            if done % save_interval == 0:
                save_fundamentals_cache(cache)
            # Progress updates every 25 to keep the UI lively without spam
            if progress and (done % 25 == 0 or done == len(missing)):
                progress(
                    "industry", done, len(missing),
                    f"Industry backfill: {done}/{len(missing)} ({n_new} ok, {n_fail} miss)",
                )

    # Final save
    save_fundamentals_cache(cache)

    if progress:
        progress(
            "industry", len(missing), len(missing),
            f"Industry backfill complete: +{n_new} industries cached ({n_fail} unresolved)",
        )

    return n_new, n_fail


def fetch_fundamentals_sector_mktcap(stock_cache: dict, progress: ProgressCallback = None):
    """Fetch sector / industry / market cap / PEG / earnings + growth flags.

    Three big optimisations vs. the original serial loop:
      1. Reads from data/cache/fundamentals.json first. Per-field TTLs:
         static (sector/industry) = 90d, mktcap = 30d, growth (PEG / EPS /
         Sale / earnings_date) = 5d. Most days, most fields stay cached.
      2. Issues only the network calls that are actually needed (info /
         quarterly_financials / earnings_date) based on which fields are stale.
      3. Runs the per-ticker fetches via ThreadPoolExecutor (default 8
         workers) with exponential backoff on HTTP 429 / rate-limit errors.
         The worker pool dynamically shrinks if 429s start landing.

    Safety nets:
      - Cache reads / writes never throw; corrupt cache = cold cache.
      - Per-ticker fetch failures fall back to whatever was previously cached
        (even if stale) so a 429 storm degrades gracefully rather than
        wiping the dataset.
      - All outputs are populated for every ticker in stock_cache; missing
        data is filled with "N/A" / None so downstream code never KeyErrors.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scanner.data_cache import (
        load_fundamentals_cache, save_fundamentals_cache,
        get_cached_fundamentals, need_refresh, update_fundamentals_entry,
    )

    global _FUND_MAX_WORKERS
    _FUND_MAX_WORKERS = 4  # safer default — 12 triggers Yahoo rate limit cascade
                            # at scale (~454 tickers). 4 keeps us under the wire
                            # at the cost of a longer fundamentals stage.

    cache = load_fundamentals_cache()

    fund_map: dict[str, str] = {}
    sector_map: dict[str, tuple] = {}
    mktcap_map: dict[str, str] = {}
    earnings_map: dict[str, Optional[str]] = {}
    peg_map: dict[str, Optional[float]] = {}
    inst_map: dict[str, Optional[float]] = {}  # heldPercentInstitutions, 0-100 scale

    def _hydrate_from_entry(ticker: str, entry: dict):
        """Populate the output maps for one ticker from its cache entry."""
        sector_short = entry.get("sector_short", entry.get("sector", "N/A")) or "N/A"
        industry = entry.get("industry", "N/A") or "N/A"
        sector_map[ticker] = (sector_short, industry)
        mktcap_map[ticker] = entry.get("mktcap_display", "N/A") or "N/A"
        peg_map[ticker] = entry.get("peg")
        earnings_map[ticker] = entry.get("earnings_date")
        fund_map[ticker] = entry.get("fund_display") or "N/A"
        inst_map[ticker] = entry.get("held_pct_institutions")

    # ---- Partition tickers into "fully cached" vs "needs fetch" ----
    needs_fetch: list[tuple[str, bool, bool, bool]] = []  # (ticker, ns, nm, ng)
    n_fully_cached = 0
    for ticker in stock_cache:
        entry = get_cached_fundamentals(ticker, cache)
        need_static = need_refresh(entry, "static")
        need_mktcap = need_refresh(entry, "mktcap")
        need_growth = need_refresh(entry, "growth")

        # Always hydrate maps with whatever we have cached (even stale
        # data is better than "N/A" if the fetch later fails).
        if entry:
            _hydrate_from_entry(ticker, entry)
        else:
            fund_map[ticker] = "N/A"
            sector_map[ticker] = ("N/A", "N/A")
            mktcap_map[ticker] = "N/A"
            earnings_map[ticker] = None
            peg_map[ticker] = None
            inst_map[ticker] = None

        if need_static or need_mktcap or need_growth:
            needs_fetch.append((ticker, need_static, need_mktcap, need_growth))
        else:
            n_fully_cached += 1

    total_to_fetch = len(needs_fetch)
    if progress:
        progress(
            "fundamentals", 0, max(1, total_to_fetch),
            f"Fundamentals: {n_fully_cached} cached / {total_to_fetch} need refresh...",
        )

    # ---- Threaded fetch with backoff ----
    if needs_fetch:
        workers = max(1, min(_FUND_MAX_WORKERS, total_to_fetch))
        completed = 0
        failures = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_fetch_one_fundamentals, t, ns, nm, ng): t
                for (t, ns, nm, ng) in needs_fetch
            }
            for fut in as_completed(futures):
                ticker = futures[fut]
                completed += 1
                if progress and completed % 25 == 0:
                    progress(
                        "fundamentals", completed, total_to_fetch,
                        f"Fundamentals: {completed}/{total_to_fetch} ({failures} failures, {workers}w)",
                    )
                try:
                    result = fut.result()
                    if result is None:
                        failures += 1
                        continue
                    static_data, mktcap_data, growth_data = result
                    if static_data is None and mktcap_data is None and growth_data is None:
                        failures += 1
                        continue
                    update_fundamentals_entry(
                        cache, ticker,
                        static=static_data,
                        mktcap=mktcap_data,
                        growth=growth_data,
                    )
                    _hydrate_from_entry(ticker, cache[ticker])
                except Exception:
                    failures += 1
                    continue

    # Persist the updated cache. Best-effort — a failed write just means
    # next scan re-fetches everything; correctness is preserved.
    save_fundamentals_cache(cache)

    if progress:
        progress("fundamentals", max(1, total_to_fetch), max(1, total_to_fetch),
                 f"Fundamentals complete ({n_fully_cached} from cache, {total_to_fetch} fetched)")
    return fund_map, sector_map, mktcap_map, earnings_map, peg_map, inst_map


def rank_industries(rs_map: dict, sector_map: dict) -> dict:
    """Rank industry groups by mean RS rating."""
    rows = []
    for ticker, rs in rs_map.items():
        sector_industry = sector_map.get(ticker)
        if not sector_industry or sector_industry == ("N/A", "N/A"):
            continue
        _, industry = sector_industry
        if not industry or industry == "N/A":
            continue
        rows.append({"Industry": industry, "RS": rs})
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    grp = df.groupby("Industry").agg(
        mean_rs=("RS", "mean"),
        n_stocks=("RS", "count"),
    ).reset_index()
    grp = grp[grp["n_stocks"] >= 3]
    grp = grp.sort_values("mean_rs", ascending=False).reset_index(drop=True)
    out = {}
    for rank, row in enumerate(grp.itertuples(index=False), start=1):
        out[row.Industry] = (rank, round(float(row.mean_rs), 1), int(row.n_stocks))
    return out


def _build_signals_warnings(
    *,
    breakout: dict,
    power_play: dict,
    rs_new_high: bool,
    vcp: dict,
    extension: dict,
    days_to_earnings: Optional[int],
    risk_pct: float,
    squat_today: bool,
    cheat: Optional[dict] = None,
    gap_up: Optional[dict] = None,
    pressure_20d: float = 1.0,
    range_ratio_5v20: float = 1.0,
    ma_target: Optional[dict] = None,
    avg_dollar_volume_20d: float = 0.0,
) -> tuple[list[str], list[str]]:
    """Build the green-signal + red-warning tag lists for a candidate.

    Signal priority order (left-to-right in the table):
      BUY ZONE → BO TODAY → CHEAT → POWER PLAY → GAP UP → RS✓ → VCP-A → VCP-B

    BUY ZONE (added 2026-06-30, fully reverted to original criteria after
    over-tightening broke the scan):
      Anticipatory pre-breakout entry window. Fires when ALL FOUR are true:
        - Trend template intact (free — every Momentum candidate has this)
        - Pressure_20d >= 1.3 (20-day up-vol/down-vol confirms accumulation)
        - Pct_Above_50MA in [-3, +10] (near support, not chasing)
        - VCP_Quality is A, B, or C (some pattern structure exists)

      REVERTED 2026-06-30 (twice same day):
        - The 5d-vs-20d range-ratio gate cut legitimate A-grade setups (AXGN,
          NVT, ESI) because 50MA-test-and-bounce setups have elevated recent
          range by definition. Removed.
        - The M&A-target gate was reverted alongside the min-history gate
          when the latter cut the universe from 3,228 → 705. M&A detection
          stays available as a helper function but no longer gates BUY ZONE.
      Per user's cardinal rule: maximum recall, manually sift false positives.
    """
    signals = []
    warnings_list = []
    # BUY ZONE — anticipatory composite, surfaced FIRST in the priority order
    # because it's the highest-leverage entry signal (low risk, before crowd).
    # $20M avg-dollar-volume gate added 2026-07-02 after user feedback: SIF /
    # GPRE / MXL-class micro-caps with 6-figure daily notional were consistently
    # the worst-quality candidates in every batch. Cutting at $20M ADV kills
    # those at the source without hurting the mid-cap-and-up universe where
    # institutional flow actually lives.
    _MIN_BUY_ZONE_DOLLAR_VOLUME = 20_000_000
    pct_50ma = extension.get("pct_above_50ma", 0.0) if extension else 0.0
    vcp_quality = vcp.get("quality") if vcp else None
    in_buy_zone = (
        pressure_20d >= 1.3
        and -3.0 <= pct_50ma <= 10.0
        and vcp_quality in ("A", "B", "C")
        and avg_dollar_volume_20d >= _MIN_BUY_ZONE_DOLLAR_VOLUME
    )
    if in_buy_zone:
        signals.append("BUY ZONE")
    if breakout.get("is_breakout"):
        signals.append("BO TODAY")
    if cheat and cheat.get("is_cheat"):
        signals.append("CHEAT")
    if power_play.get("is_power_play"):
        signals.append("POWER PLAY")
    if gap_up and gap_up.get("is_gap_up"):
        signals.append("GAP UP")
    if rs_new_high:
        signals.append("RS✓")
    if vcp.get("quality") == "A":
        signals.append("VCP-A")
    elif vcp.get("quality") == "B":
        signals.append("VCP-B")
    if extension.get("is_extended"):
        warnings_list.append("EXT")
    if days_to_earnings is not None and 0 <= days_to_earnings <= 7:
        warnings_list.append(f"ER {days_to_earnings}d")
    if risk_pct > 7.0:
        warnings_list.append("WIDE")
    if squat_today:
        warnings_list.append("SQUAT")
    return signals, warnings_list


def run_filters(
    stock_cache: dict,
    rs_map: dict,
    fund_map: dict,
    sector_map: dict,
    mktcap_map: dict,
    earnings_map: dict,
    industry_ranks: dict,
    sp500_set: set[str],
    nq100_set: set[str],
    spy_hist: pd.DataFrame,
    settings: ScanSettings,
    progress: ProgressCallback = None,
    peg_map: Optional[dict] = None,
    inst_map: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    if peg_map is None:
        peg_map = {}
    if inst_map is None:
        inst_map = {}
    rs_threshold = 85 if settings.elite_mode else 70
    candidates: list[dict] = []
    d_rs = d_trend = d_funds = d_other = 0
    total = len(stock_cache)

    for i, (ticker, data) in enumerate(stock_cache.items()):
        if progress and i % 50 == 0:
            progress("filter", i, total, f"Filtering ({i}/{total})")
        try:
            rs = rs_map.get(ticker, 0)
            if rs < rs_threshold:
                d_rs += 1
                continue
            # NOTE: a min-history gate (len(data) < 220) was tried 2026-06-30
            # and REVERTED same day — it cut the universe from 3,228 → 705
            # and BUY ZONE candidates from 17 → 3 because much of the price
            # cache has shorter history than expected. The XMAX/TDAY false
            # positives are accepted as a cost of keeping recall high, per
            # user's cardinal rule (better to manually sift trash than to
            # accidentally miss good setups).
            close = float(data["Close"].iloc[-1])
            sma_50 = data["Close"].rolling(50).mean().iloc[-1]
            sma_150 = data["Close"].rolling(150).mean().iloc[-1]
            sma_200 = data["Close"].rolling(200).mean().iloc[-1]
            low_52 = data["Close"].tail(252).min()
            high_52 = data["Close"].tail(252).max()
            if not (
                close > sma_50
                and close > sma_150
                and close > sma_200
                and sma_50 > sma_150
                and sma_150 > sma_200
            ):
                d_trend += 1
                continue
            if not (close > (1.3 * low_52) and close > (0.75 * high_52)):
                d_trend += 1
                continue
            sma_200_20_ago = data["Close"].rolling(200).mean().shift(20).iloc[-1]
            if sma_200 <= sma_200_20_ago:
                d_trend += 1
                continue
            fund_display = fund_map.get(ticker, "N/A")
            if settings.elite_mode and "EPS+" not in fund_display and "Sale+" not in fund_display:
                d_funds += 1
                continue

            contractions = detect_vcp_contractions(data, lookback=80)
            vcp_quality = vcp_quality_score(contractions)
            pivot_info = find_pivot_point(data, lookback=60)
            if pivot_info is None:
                d_other += 1
                continue
            pivot_price, pivot_date = pivot_info
            atr_20 = calc_atr(data, period=20)
            stop_info = calculate_smart_stop(data, pivot_price, atr_20, sma_50=float(sma_50))
            buy_trigger = round(pivot_price * 1.005, 2)
            natural_stop = stop_info["stop"]
            risk_per_share = buy_trigger - natural_stop
            if risk_per_share <= 0:
                d_other += 1
                continue
            dollar_risk = settings.account_size * settings.risk_per_trade
            shares = int(dollar_risk / risk_per_share)
            risk_pct = (risk_per_share / buy_trigger) * 100

            curr_vol = data["Volume"].iloc[-1]
            avg_vol_50 = data["Volume"].rolling(50).mean().iloc[-1]
            if curr_vol < (avg_vol_50 * 0.5):
                vdu_display = "Extreme"
            elif curr_vol < avg_vol_50:
                vdu_display = "Yes"
            else:
                vdu_display = "No"

            try:
                aligned_spy = spy_hist["Close"].reindex(data.index).ffill()
                rs_line = data["Close"] / aligned_spy
                rs_line_high = rs_line.tail(50).max()
                rs_new_high_bool = rs_line.iloc[-1] >= (rs_line_high * 0.99)
            except Exception:
                rs_new_high_bool = False

            # RS-LEADING-PRICE divergence (the cleanest "early" signal we can
            # detect without building EARLY mode). When the RS line makes a
            # new 50-day high BUT price hasn't yet — that's institutions
            # accumulating ahead of the obvious breakout. Surfaced as
            # "RS_Leading" so the user can spot pre-breakout accumulation.
            try:
                price_50d_high = data["Close"].tail(50).max()
                # Price is "at new high" if within 2% of recent 50d high
                price_at_new_high = close >= price_50d_high * 0.98
                rs_leading_bool = rs_new_high_bool and not price_at_new_high
            except Exception:
                rs_leading_bool = False

            power_play = detect_power_play(data)
            breakout = detect_breakout_today(data, pivot_price, float(avg_vol_50))
            extension = detect_extension_from_50ma(close, float(sma_50))
            # CHEAT and GAP UP — added 2026-06-30 as Minervini-style early-entry
            # and catalyst-day setups. CHEAT needs the VCP contraction list; if
            # no qualifying VCP, it returns is_cheat=False harmlessly.
            cheat = detect_cheat_setup(
                data, vcp_quality.get("contractions") or [],
                pivot_price, float(avg_vol_50),
            )
            gap_up = detect_gap_up_today(data, float(avg_vol_50))

            # 20-day Pressure (Minervini accumulation gauge) — sum of volume on
            # up days vs down days over the last 20 sessions. Ratio > 1 means
            # institutional accumulation; the actual signal threshold for
            # BUY ZONE is 1.3+. Cheap to compute (~0.1ms per ticker).
            pressure_20d = 1.0
            try:
                if len(data) >= 21:
                    last_21 = data.tail(21)
                    closes = last_21["Close"]
                    vols   = last_21["Volume"]
                    diff   = closes.diff()
                    up_vol_sum   = float(vols[diff > 0].sum())
                    down_vol_sum = float(vols[diff < 0].sum())
                    if down_vol_sum > 0:
                        pressure_20d = round(up_vol_sum / down_vol_sum, 2)
                    elif up_vol_sum > 0:
                        # All up-days in the last 20 — saturate at a high value
                        # rather than reporting infinity. 5.0 reads cleanly as
                        # "very strong accumulation."
                        pressure_20d = 5.0
            except Exception:
                pass

            # 20-day average daily DOLLAR volume — used as a liquidity gate on
            # BUY ZONE. Rationale (added 2026-07-02 after user feedback): thin
            # names (SIF / GPRE / MXL-class 6-figure-volume micro-caps) with
            # $500K daily notional consistently showed up in BUY ZONE and were
            # the worst-quality candidates in every batch. $20M threshold cuts
            # them at the source. Computed as mean(Close × Volume) over the
            # last 20 sessions. Zero if history is too short (blocks BUY ZONE).
            avg_dollar_volume_20d = 0.0
            try:
                if len(data) >= 20:
                    last_20 = data.tail(20)
                    dv = (last_20["Close"] * last_20["Volume"]).astype(float)
                    avg_dollar_volume_20d = float(dv.mean())
            except Exception:
                pass

            # 5-day vs prior-20-day daily-range ratio (volatility contraction
            # check for BUY ZONE — added 2026-06-30 after AXSM false positive
            # where a V-shape recovery passed criteria despite wild whipsaws).
            # Ratio ≤ 1.0 = volatility contracting/flat (constructive base).
            # Ratio > 1.3 = expanding (potential climax or post-shake-out chop).
            range_ratio_5v20 = 1.0
            try:
                if len(data) >= 25:
                    daily_range = (data["High"] - data["Low"]).abs()
                    last5 = float(daily_range.tail(5).mean())
                    prior20 = float(daily_range.iloc[-25:-5].mean())
                    if prior20 > 0:
                        range_ratio_5v20 = round(last5 / prior20, 2)
            except Exception:
                pass

            # M&A target check — flag stocks with single >30% gap + post-gap
            # range collapse. Filters out OGN-class buyout setups that look
            # like tight high-tight flags but are dead-money arbitrage.
            ma_target = detect_ma_target(data)

            squat_today = False
            try:
                day_high = float(data["High"].iloc[-1])
                day_low = float(data["Low"].iloc[-1])
                day_close = close
                recent_hi = float(data["High"].iloc[-11:-1].max())
                if day_high > recent_hi:
                    rng = day_high - day_low
                    if rng > 0 and ((day_close - day_low) / rng) < 0.50:
                        squat_today = True
            except Exception:
                pass

            days_to_er = days_until(earnings_map.get(ticker))
            signals, warnings_list = _build_signals_warnings(
                breakout=breakout, power_play=power_play,
                rs_new_high=rs_new_high_bool, vcp=vcp_quality,
                extension=extension, days_to_earnings=days_to_er,
                risk_pct=risk_pct, squat_today=squat_today,
                cheat=cheat, gap_up=gap_up,
                pressure_20d=pressure_20d,
                range_ratio_5v20=range_ratio_5v20,
                ma_target=ma_target,
                avg_dollar_volume_20d=avg_dollar_volume_20d,
            )

            sector_val, industry_val = sector_map.get(ticker, ("N/A", "N/A"))
            ind_rank_data = industry_ranks.get(industry_val)
            industry_rank = ind_rank_data[0] if ind_rank_data else None

            t_clean = ticker.replace("-", ".")
            if t_clean in sp500_set or ticker in sp500_set:
                index_label = "S&P500"
            elif t_clean in nq100_set or ticker in nq100_set:
                index_label = "NQ100"
            else:
                index_label = "RUT"

            rsi_val = calc_rsi(data["Close"])

            candidates.append({
                "Ticker": ticker,
                "Index": index_label,
                "Market_Cap": mktcap_map.get(ticker, "N/A"),
                "Sector": sector_val,
                "Industry": industry_val,
                "Industry_Rank": industry_rank,
                "RS": int(rs),
                "RSI": rsi_val,
                "PEG": peg_map.get(ticker),
                "Held_Pct_Inst": inst_map.get(ticker),
                "Avg_Volume": int(data["Volume"].rolling(50).mean().iloc[-1]) if len(data) >= 50 else None,
                "RS_NH": "YES" if rs_new_high_bool else "-",
                # RS line at new high while price still inside its base —
                # institutional accumulation signal BEFORE the obvious breakout.
                "RS_Leading": "YES" if rs_leading_bool else "-",
                "VCP_Quality": vcp_quality.get("quality", "None"),
                "VCP_Count": vcp_quality.get("count", 0),
                "VDU": vdu_display,
                "Pct_Above_50MA": extension.get("pct_above_50ma", 0.0),
                "Pressure_20d": pressure_20d,
                # Persisted so BUY ZONE cohort log can capture the liquidity
                # environment at flag time — useful for post-hoc analysis of
                # whether tighter/looser thresholds would have improved hit rate.
                "Avg_Dollar_Volume_20d": round(avg_dollar_volume_20d, 0),
                "Power_Play": power_play.get("is_power_play", False),
                "Breakout_Today": breakout.get("is_breakout", False),
                "Vol_vs_Avg": breakout.get("vol_ratio", 0.0),
                "Fundamentals": fund_display,
                "Price": round(close, 2),
                "Buy_Trigger": buy_trigger,
                "Stop": natural_stop,
                "Stop_Reason": stop_info.get("reason", ""),
                "ATR_20": atr_20 if atr_20 is not None else 0.0,
                "Risk_Pct": round(risk_pct, 2),
                "Risk_Exceeds_Max": risk_pct > settings.max_stop_allowed * 100,
                "Shares": int(shares),
                "Days_To_Earnings": days_to_er,
                "Earnings_Date": earnings_map.get(ticker),
                "Signals": signals,
                "Warnings": warnings_list,
            })
        except Exception:
            d_other += 1
            continue

    diagnostics = {
        "failed_rs": d_rs,
        "failed_trend": d_trend,
        "failed_fundamentals": d_funds,
        "other": d_other,
        "passed": len(candidates),
    }
    if progress:
        progress("filter", total, total, f"Filter complete — {len(candidates)} candidates")
    return candidates, diagnostics


def run_scan(settings: ScanSettings, progress: ProgressCallback = None) -> ScanResult:
    timestamp = datetime.utcnow().isoformat() + "Z"
    if progress:
        progress("market", 0, 1, "Diagnosing market context…")
    (
        market_signal, sp_trend, sp_dist,
        nq_trend, nq_dist, rut_trend, rut_dist,
        sp_dist_dates, nq_dist_dates, rut_dist_dates,
        market_state,
    ) = diagnose_market_context()
    if progress:
        progress("market", 1, 1, f"Market: {market_signal}")

    if settings.ticker_subset is not None:
        if progress:
            progress("tickers", 0, 1, f"Using {len(settings.ticker_subset)} tickers from subset")
        tickers = list(settings.ticker_subset)
        sp500_set = fetch_sp500_set()
        nq100_set = fetch_nq100_set()
    else:
        tickers, sp500_set, nq100_set = collect_tickers(progress)

    if progress:
        progress("spy", 0, 1, "Downloading SPY benchmark…")
    spy_hist = yf.Ticker("SPY").history(
        start=(datetime.now() - timedelta(days=540)).strftime("%Y-%m-%d"),
        interval="1d",
    )
    spy_hist = clean_history(spy_hist)

    # Persist SPY to the shared price cache so other scans (EARLY especially)
    # can use a fresh benchmark. Previously SPY was downloaded for the scan
    # but never saved, leaving the price cache's SPY.pkl stale by weeks.
    # User-caught bug 2026-06-09: EARLY mode was using May 21 SPY data
    # because Momentum had been refreshing every other ticker but not SPY.
    try:
        from .data_cache import save_price_history
        save_price_history("SPY", spy_hist)
    except Exception:
        pass  # best-effort — don't fail the scan if cache write fails

    stock_cache, rs_df, rs_map, skipped_history, skipped_liquidity = download_history_and_rs(
        tickers, settings, progress=progress,
    )

    if progress:
        progress("breadth", 0, 1, "Calculating breadth…")
    (pct_above_50, pct_above_200, up_down_ratio, new_highs, new_lows,
     breadth_status, pct_above_50_10d_ago, narrowing) = calc_breadth(
        stock_cache, sp_trend, nq_trend, rut_trend,
    )

    try:
        prime_sector_from_ishares(progress=progress)
    except Exception:
        pass

    if progress:
        progress("industry", 0, 1, "Checking industry universe coverage…")
    try:
        fetch_industry_only(list(rs_map.keys()), progress=progress, max_new=None)
    except Exception:
        pass

    rs_threshold = 85 if settings.elite_mode else 70
    pre_filter_count = len(stock_cache)
    stock_cache = {t: d for t, d in stock_cache.items()
                   if rs_map.get(t, 0) >= rs_threshold}
    if progress:
        progress("fundamentals", 0, 1,
                 f"RS pre-filter: {len(stock_cache)} of {pre_filter_count} tickers passed RS>={rs_threshold} threshold")

    fund_map, sector_map, mktcap_map, earnings_map, peg_map, inst_map = fetch_fundamentals_sector_mktcap(
        stock_cache, progress=progress,
    )

    if progress:
        progress("groups", 0, 1, "Ranking industry groups…")
    industry_ranks = rank_industries(rs_map, sector_map)

    mode_label = "ELITE SNIPER" if settings.elite_mode else "GRAND MASTER"

    candidates, diagnostics = run_filters(
        stock_cache, rs_map, fund_map, sector_map, mktcap_map, earnings_map,
        industry_ranks, sp500_set, nq100_set, spy_hist, settings,
        progress=progress, peg_map=peg_map, inst_map=inst_map,
    )

    if progress:
        progress("done", 1, 1, "Scan complete")

    from scanner.data_cache import load_fundamentals_cache
    fund_cache = load_fundamentals_cache()
    industry_universe_rows = []
    for ticker, rs in rs_map.items():
        entry = fund_cache.get(ticker) or {}
        sector_val = entry.get("sector_short") or entry.get("sector") or "N/A"
        industry_val = entry.get("industry") or "N/A"
        industry_universe_rows.append({
            "Ticker": ticker,
            "RS": int(rs),
            "Industry": industry_val,
            "Sector": sector_val,
        })

    return ScanResult(
        timestamp=timestamp,
        settings=asdict(settings),
        market_signal=market_signal,
        sp_trend=sp_trend,
        nq_trend=nq_trend,
        rut_trend=rut_trend,
        sp_dist=sp_dist,
        nq_dist=nq_dist,
        rut_dist=rut_dist,
        sp_dist_dates=sp_dist_dates,
        nq_dist_dates=nq_dist_dates,
        rut_dist_dates=rut_dist_dates,
        market_state=market_state,
        universe_size=pre_filter_count,
        pct_above_50=round(pct_above_50, 1),
        pct_above_200=round(pct_above_200, 1),
        up_down_ratio=round(up_down_ratio, 2),
        new_highs=new_highs,
        new_lows=new_lows,
        breadth_status=breadth_status,
        pct_above_50_10d_ago=round(pct_above_50_10d_ago, 1),
        narrowing=narrowing,
        rs_threshold=rs_threshold,
        mode_label=mode_label,
        candidates=candidates,
        diagnostics=diagnostics,
        skipped_history=skipped_history,
        skipped_liquidity=skipped_liquidity,
        industry_ranks=industry_ranks,
        industry_universe=industry_universe_rows,
    )
