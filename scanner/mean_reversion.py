"""
Mean Reversion scanner — three counter-trend setups on QUALITY-filtered stocks.

Philosophy:
    Mean reversion is the OPPOSITE edge from momentum. Momentum buys strength;
    mean reversion buys weakness that is statistically likely to revert. It
    works in choppy/sideways tape (when momentum fails) and gives more frequent
    trades than waiting for VCP breakouts.

    To avoid the falling-knife trap (oversold broken stocks don't reliably
    bounce), this scanner only runs on stocks that ALREADY passed a Quality
    filter (Composite Rating ≥ 70). The premise: oversold high-quality stocks
    revert; oversold junk keeps falling.

Three detectors, all gated on price > 200-day MA (long-term uptrend intact):

    1. RSI(14) OVERSOLD — classic extreme oversold (default threshold 25).
       3-7 day mean-reversion horizon. Matches the "BTC/ETH at RSI 13-15"
       inspiration — wait for an extreme reading, ride the reversion.

    2. CONNORS RSI(2) — Larry Connors' very-short-term oversold setup
       (RSI(2) < 5). 1-3 day hold. More frequent, smaller targets, requires
       active management.

    3. PULLBACK-TO-21EMA — Stage 2 stock (21EMA > 50MA > 200MA) pulling
       back to within 3% of its 21EMA on LIGHT volume (5-day avg < 80%
       of 50-day avg). The classic "buy the dip in a leader" setup.

Architecture:
    - Reads from the Quality scan + shared price cache. No new fetches.
    - One pass per ticker; all three detectors share the same MA calculations.
    - Persists to data/scans/scan_mean_reversion_*.json + latest_mean_reversion.json.

If this module is deleted, nothing else breaks.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

import pandas as pd

from .data_cache import load_price_history
from .quality import load_latest_quality_scan, QUALITY_SCANS_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MR_SCANS_DIR = QUALITY_SCANS_DIR  # share scans/ directory with other scanners
LATEST_MR_FILE = MR_SCANS_DIR / "latest_mean_reversion.json"

# Need 200 bars for the SMA200 trend gate, plus buffer for the EMAs/RSI to settle.
MIN_BARS = 220

DEFAULT_RSI14_THRESHOLD = 25.0
DEFAULT_RSI2_THRESHOLD = 5.0
DEFAULT_PULLBACK_TOLERANCE_PCT = 3.0  # within ±3% of 21EMA counts as "at MA"
DEFAULT_MIN_COMPOSITE = 70            # Quality filter floor


# ---------------------------------------------------------------------------
# Inline helpers — keep this module self-contained
# ---------------------------------------------------------------------------

def _calc_rsi(close_series: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder-style RSI via EWM. Returns most recent value or None."""
    try:
        if close_series is None or len(close_series) < period + 1:
            return None
        delta = close_series.diff()
        gain = delta.where(delta > 0, 0.0).ewm(span=period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(span=period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - 100 / (1 + rs)
        val = float(rsi.iloc[-1])
        if pd.isna(val):
            return None
        return round(val, 1)
    except Exception:
        return None


def _calc_ema(close: pd.Series, period: int) -> Optional[float]:
    try:
        if close is None or len(close) < period:
            return None
        val = float(close.ewm(span=period, adjust=False).mean().iloc[-1])
        return None if pd.isna(val) else val
    except Exception:
        return None


def _calc_sma(close: pd.Series, period: int) -> Optional[float]:
    try:
        if close is None or len(close) < period:
            return None
        val = float(close.tail(period).mean())
        return None if pd.isna(val) else val
    except Exception:
        return None


def _flatten_series(price_df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    """Defensively flatten multi-index columns from batched yfinance downloads.
    Returns None if the column doesn't exist."""
    if price_df is None or col not in price_df.columns:
        return None
    s = price_df[col]
    if hasattr(s, "columns") or s.ndim > 1:
        s = s.iloc[:, 0]
    return s


def _format_mktcap(v) -> str:
    """Format raw market cap → $1.5T / $37.3B / $250M / N/A."""
    try:
        if v is None or v == 0:
            return "N/A"
        v = float(v)
        abs_v = abs(v)
        if abs_v >= 1e12: return f"${v/1e12:.2f}T"
        if abs_v >= 1e9:  return f"${v/1e9:.1f}B"
        if abs_v >= 1e6:  return f"${v/1e6:.0f}M"
        if abs_v >= 1e3:  return f"${v/1e3:.0f}K"
        return f"${v:.0f}"
    except Exception:
        return "N/A"


# ---------------------------------------------------------------------------
# Detectors — each returns a small dict of flags + values
# ---------------------------------------------------------------------------

def detect_rsi14_oversold(close: pd.Series, sma200: Optional[float],
                            rsi14_threshold: float = DEFAULT_RSI14_THRESHOLD) -> dict:
    """RSI(14) extreme oversold in a long-term uptrend (above 200MA).

    Signal fires when:
      - RSI(14) < threshold (default 25)
      - Price > 200-day SMA (trend intact)
    """
    out = {
        "rsi14": None,
        "pct_above_200ma": None,
        "above_200ma": False,
        "rsi14_signal": False,
    }
    if close is None or len(close) < 15:
        return out
    rsi = _calc_rsi(close, 14)
    if rsi is not None:
        out["rsi14"] = rsi
    price = float(close.iloc[-1])
    if sma200 is not None and sma200 > 0:
        out["pct_above_200ma"] = round((price / sma200 - 1.0) * 100, 2)
        out["above_200ma"] = bool(price > sma200)
    out["rsi14_signal"] = bool(
        rsi is not None
        and rsi < rsi14_threshold
        and out["above_200ma"]
    )
    return out


def detect_connors_rsi2(close: pd.Series, sma200: Optional[float],
                         rsi2_threshold: float = DEFAULT_RSI2_THRESHOLD) -> dict:
    """Larry Connors' RSI(2) very-short-term oversold setup.

    Signal fires when:
      - RSI(2) < threshold (default 5 — Connors' canonical value)
      - Price > 200-day SMA
    """
    out = {
        "rsi2": None,
        "connors_signal": False,
    }
    if close is None or len(close) < 3:
        return out
    rsi = _calc_rsi(close, 2)
    if rsi is not None:
        out["rsi2"] = rsi
    price = float(close.iloc[-1])
    above_200 = bool(sma200 is not None and sma200 > 0 and price > sma200)
    out["connors_signal"] = bool(
        rsi is not None and rsi < rsi2_threshold and above_200
    )
    return out


def detect_pullback_to_ma(close: pd.Series, volume: Optional[pd.Series],
                            sma200: Optional[float], sma50: Optional[float],
                            ema21: Optional[float],
                            pullback_tolerance_pct: float = DEFAULT_PULLBACK_TOLERANCE_PCT) -> dict:
    """Stage 2 stock pulling back to the 21EMA on light volume.

    Signal fires when:
      - Stage 2 stack: 21EMA > 50MA > 200MA AND price > 200MA
      - Price within ±tolerance% of 21EMA (touched or just bounced)
      - Volume drying up: 5-day avg vol < 80% of 50-day avg vol
        (pullback on light volume = healthy; heavy volume = distribution)
    """
    out = {
        "pct_from_21ema": None,
        "pct_from_50ma": None,
        "stage2_stack": False,
        "vol_drying": False,
        "pullback_signal": False,
    }
    if close is None or sma200 is None or sma50 is None or ema21 is None:
        return out
    if len(close) < 50:
        return out

    price = float(close.iloc[-1])

    # Stage 2 stack — Minervini's textbook uptrend confirmation
    stage2 = bool(ema21 > sma50 > sma200 and price > sma200)
    out["stage2_stack"] = stage2

    # Distance from MAs (negative = below)
    if ema21 > 0:
        out["pct_from_21ema"] = round((price / ema21 - 1.0) * 100, 2)
    if sma50 > 0:
        out["pct_from_50ma"] = round((price / sma50 - 1.0) * 100, 2)

    near_21ema = bool(
        out["pct_from_21ema"] is not None
        and abs(out["pct_from_21ema"]) <= pullback_tolerance_pct
    )

    # Volume drying — 5-day avg volume < 80% of 50-day avg
    if volume is not None and len(volume) >= 50:
        try:
            avg_5 = float(volume.tail(5).mean())
            avg_50 = float(volume.tail(50).mean())
            if avg_50 > 0:
                out["vol_drying"] = bool(avg_5 < avg_50 * 0.8)
        except Exception:
            pass

    out["pullback_signal"] = bool(stage2 and near_21ema and out["vol_drying"])
    return out


# ---------------------------------------------------------------------------
# Result + persistence
# ---------------------------------------------------------------------------

@dataclass
class MeanReversionResult:
    """One pass of the Mean Reversion scan."""
    timestamp: str = ""
    universe_size: int = 0       # Quality candidates passing Composite filter
    scanned: int = 0             # Tickers we actually computed signals on
    candidates: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write via .tmp + os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_mean_reversion_scan(
    rsi14_threshold: float = DEFAULT_RSI14_THRESHOLD,
    rsi2_threshold: float = DEFAULT_RSI2_THRESHOLD,
    pullback_tolerance_pct: float = DEFAULT_PULLBACK_TOLERANCE_PCT,
    min_composite: int = DEFAULT_MIN_COMPOSITE,
    progress: Optional[Callable] = None,
) -> MeanReversionResult:
    """Load Quality scan, filter to Composite ≥ min_composite, run all three
    detectors per ticker, persist result.

    progress signature: progress(stage: str, completed: int, total: int, msg: str)
    """
    result = MeanReversionResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        settings={
            "rsi14_threshold": rsi14_threshold,
            "rsi2_threshold": rsi2_threshold,
            "pullback_tolerance_pct": pullback_tolerance_pct,
            "min_composite": min_composite,
        },
    )

    if progress:
        progress("init", 0, 1, "Loading Quality scan…")

    # 1. Load the Quality universe
    quality = load_latest_quality_scan() or {}
    quality_candidates = quality.get("candidates") or []
    if not quality_candidates:
        result.diagnostics["error"] = (
            "No Quality scan found. Run a Quality scan first — this scanner "
            "only operates on stocks that already passed quality filters."
        )
        return result

    # 2. Filter to Composite ≥ min_composite
    quality_lookup: dict[str, dict] = {}
    for c in quality_candidates:
        ticker = c.get("Ticker")
        comp = c.get("Composite_Rating") or 0
        if ticker and comp >= min_composite:
            quality_lookup[ticker] = c

    result.universe_size = len(quality_lookup)
    if not quality_lookup:
        result.diagnostics["error"] = (
            f"No Quality candidates with Composite ≥ {min_composite}. "
            f"Lower the floor or rerun the Quality scan."
        )
        return result

    if progress:
        progress("scan", 0, len(quality_lookup),
                 f"Scanning {len(quality_lookup)} Quality stocks for mean-reversion signals…")

    # 3. Walk each ticker, run detectors
    tickers = sorted(quality_lookup.keys())
    diag = {
        "rsi14_signal_count":     0,
        "connors_signal_count":   0,
        "pullback_signal_count":  0,
        "any_signal_count":       0,
        "skipped_no_price_data":  0,
        "skipped_insufficient_history": 0,
        "exceptions":             0,
    }
    candidates: list[dict] = []
    for i, ticker in enumerate(tickers):
        if progress and i % 100 == 0:
            progress("scan", i, len(tickers),
                     f"Scanned {i}/{len(tickers)} — {len(candidates)} signals so far")

        try:
            price_df = load_price_history(ticker)
            if price_df is None or price_df.empty:
                diag["skipped_no_price_data"] += 1
                continue
            close = _flatten_series(price_df, "Close")
            volume = _flatten_series(price_df, "Volume")
            if close is None or len(close) < MIN_BARS:
                diag["skipped_insufficient_history"] += 1
                continue

            # Pre-compute MAs once per ticker (shared across detectors)
            sma200 = _calc_sma(close, 200)
            sma50  = _calc_sma(close, 50)
            ema21  = _calc_ema(close, 21)

            d1 = detect_rsi14_oversold(close, sma200, rsi14_threshold)
            d2 = detect_connors_rsi2(close, sma200, rsi2_threshold)
            d3 = detect_pullback_to_ma(close, volume, sma200, sma50, ema21,
                                        pullback_tolerance_pct)

            if d1["rsi14_signal"]:    diag["rsi14_signal_count"]    += 1
            if d2["connors_signal"]:  diag["connors_signal_count"]  += 1
            if d3["pullback_signal"]: diag["pullback_signal_count"] += 1

            if not (d1["rsi14_signal"] or d2["connors_signal"] or d3["pullback_signal"]):
                continue
            diag["any_signal_count"] += 1

            # Enrich with Quality metadata + current price
            q = quality_lookup[ticker]
            price = float(close.iloc[-1])
            candidates.append({
                "Ticker":              ticker,
                "Price":               round(price, 2),
                "RSI_14":              d1["rsi14"],
                "RSI_2":               d2["rsi2"],
                "Pct_Above_200MA":     d1["pct_above_200ma"],
                "Pct_From_50MA":       d3["pct_from_50ma"],
                "Pct_From_21EMA":      d3["pct_from_21ema"],
                "Vol_Drying":          "YES" if d3["vol_drying"] else "-",
                "Stage2_Stack":        "YES" if d3["stage2_stack"] else "-",
                "Above_200MA":         "YES" if d1["above_200ma"] else "-",
                "RSI14_Signal":        "YES" if d1["rsi14_signal"] else "-",
                "Connors_Signal":      "YES" if d2["connors_signal"] else "-",
                "Pullback_Signal":     "YES" if d3["pullback_signal"] else "-",
                "Composite_Rating":    q.get("Composite_Rating"),
                "EPS_Rating":          q.get("EPS_Rating"),
                "Sector":              q.get("Sector") or "N/A",
                "Industry":            q.get("Industry") or "N/A",
                "Market_Cap":          _format_mktcap(q.get("Mktcap_Raw")),
                "Mktcap_Raw":          q.get("Mktcap_Raw"),
            })
        except Exception:
            diag["exceptions"] += 1
            continue

    result.scanned = len(tickers)
    result.candidates = candidates
    result.diagnostics = diag

    if progress:
        progress("done", len(tickers), len(tickers),
                 f"Mean Reversion scan complete — {len(candidates)} candidates")

    # 4. Persist (atomic writes — same pattern as other scans)
    try:
        MR_SCANS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = result.timestamp.replace(":", "").replace("-", "").replace("T", "_")
        stamp = stamp.split("+")[0].rstrip("Z").split(".")[0]
        fname = MR_SCANS_DIR / f"scan_mean_reversion_{stamp}.json"
        body = json.dumps(asdict(result), indent=2, default=str)
        _atomic_write_text(fname, body)
        _atomic_write_text(LATEST_MR_FILE, body)
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_latest_mr_scan() -> Optional[dict]:
    """Return the most recent Mean Reversion scan as a plain dict, or None."""
    try:
        if not LATEST_MR_FILE.exists():
            return None
        raw = LATEST_MR_FILE.read_bytes().replace(b"\x00", b"")
        return json.loads(raw)
    except Exception:
        return None
