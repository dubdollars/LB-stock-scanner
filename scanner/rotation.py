"""
Industry-rotation compute layer.

Pure analytic module — given a stack of cached scan JSONs (latest +
historical), computes a per-industry rotation table:

  - current rank (by mean RS within the industry)
  - rank ~1 week ago and ~4 weeks ago
  - change in rank (positive = climbing the leaderboard)
  - mean RS, count of members, % of members above their 50MA
  - rotation score (weighted toward 4w rank change)

Layered on top of the existing scanner output — no new yfinance calls,
no new caches. The "history" is just older scan_*.json files that
scanner/cache.py already persists after each scan.

If older scans don't exist (user has only been running the scanner a
few days), rank-change columns silently come back as None and the UI
renders them as "—".
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


_REPO_ROOT = Path(__file__).resolve().parent.parent
SCANS_DIR = _REPO_ROOT / "data" / "scans"

# Filename regexes used to distinguish scan types. The Momentum prune bug
# (fixed 2026-06-17) was caused by an unfiltered glob; using explicit regexes
# here makes the source of each scan unambiguous.
_MOMENTUM_NAME_RE = re.compile(r"^scan_(\d{8})_(\d{6})\.json$")
_QUALITY_NAME_RE  = re.compile(r"^scan_quality_(\d{8})_(\d{6})\.json$")


# ---------------------------------------------------------------------------
# Scan-file loading
# ---------------------------------------------------------------------------

def _scan_timestamp(path: Path) -> Optional[datetime]:
    """Parse a Momentum-scan filename's timestamp.

    'scan_20260523_145738.json' -> datetime(2026,5,23,14,57,38, tzinfo=utc).
    Returns None for any other scan type (quality, mean_reversion, short, early).
    """
    m = _MOMENTUM_NAME_RE.match(path.name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _quality_scan_timestamp(path: Path) -> Optional[datetime]:
    """Parse a Quality-scan filename's timestamp.

    'scan_quality_20260615_174838.json' -> datetime(2026,6,15,17,48,38, tzinfo=utc).
    """
    m = _QUALITY_NAME_RE.match(path.name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# Minimum file size for a scan to be considered a valid full scan.
# Real Momentum scans are 100KB-2MB; real Quality scans are 2-4MB. A failed
# or partial scan (e.g. one that errored after only computing a few tickers)
# is typically <10KB. This threshold cleanly separates them and prevents the
# rotation merge from picking a broken file as its historical reference,
# which would silently produce empty Rank_1w/Rank_4w columns.
# User-reported bug 2026-06-26: the May 29 partial scan (3KB, 5 candidates)
# was being picked as every 4w-target match for weeks, killing 91/92 industry
# rank merges.
_MIN_SCAN_FILE_BYTES = 100_000  # 100KB


def _is_valid_scan_file(path: Path) -> bool:
    """True if the scan file is at least the minimum size to be a real scan."""
    try:
        return path.stat().st_size >= _MIN_SCAN_FILE_BYTES
    except Exception:
        return False


def list_long_scans() -> list[tuple[datetime, Path]]:
    """Return (timestamp, path) tuples for every Momentum scan on disk, newest first.
    Skips partial/failed scan files (<100KB)."""
    out = []
    if not SCANS_DIR.exists():
        return out
    for path in SCANS_DIR.glob("scan_*.json"):
        ts = _scan_timestamp(path)
        if ts is None:
            continue
        if not _is_valid_scan_file(path):
            continue
        out.append((ts, path))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def list_quality_scans() -> list[tuple[datetime, Path]]:
    """Return (timestamp, path) tuples for every Quality scan on disk, newest first.
    Skips partial/failed scan files (<100KB)."""
    out = []
    if not SCANS_DIR.exists():
        return out
    for path in SCANS_DIR.glob("scan_quality_*.json"):
        ts = _quality_scan_timestamp(path)
        if ts is None:
            continue
        if not _is_valid_scan_file(path):
            continue
        out.append((ts, path))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def find_scan_near(target_dt: datetime, tolerance_days: float = 2.5) -> Optional[Path]:
    """Find the cached Momentum scan closest to `target_dt` (within tolerance)."""
    return _find_in_list(list_long_scans(), target_dt, tolerance_days)


def find_quality_scan_near(target_dt: datetime, tolerance_days: float = 2.5) -> Optional[Path]:
    """Find the cached Quality scan closest to `target_dt` (within tolerance)."""
    return _find_in_list(list_quality_scans(), target_dt, tolerance_days)


def _find_in_list(scans: list[tuple[datetime, Path]], target_dt: datetime,
                    tolerance_days: float) -> Optional[Path]:
    """Pick the scan whose timestamp is closest to target_dt; return None if
    nothing falls within tolerance."""
    if not scans:
        return None
    best, best_diff = None, None
    for ts, path in scans:
        diff = abs((ts - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = path, diff
    if best_diff is not None and best_diff / 86400.0 <= tolerance_days:
        return best
    return None


def load_scan_json(path: Path) -> Optional[dict]:
    """Read a scan JSON. Returns None on any parse failure."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _backfill_pct_above_50ma_if_missing(scan: dict, scan_path: Path) -> None:
    """One-time backfill: compute Pct_Above_50MA for candidates missing it
    and write the mutated scan back to disk.

    Reason: the Quality scanner started populating this field on 2026-07-02,
    but scans cached before that date don't have it. The rotation UI needs
    the field for the "% > 50MA" column. Without this backfill, every call
    to industry_summary_from_quality_scan hits its price-cache fallback,
    loading ~3,000 price pkls per rerun — unusable performance.

    Strategy: if the scan looks like a legacy one (sample of candidates all
    missing the field), load each ticker's cached price history once,
    compute Pct_Above_50MA, mutate the candidate dicts in place, and save
    the scan file atomically. Subsequent loads (this session or future
    sessions) see the field present and skip fallback entirely.

    Silent no-op if:
      - Sample already has the field (post-fix scan, nothing to do)
      - Price cache can't be imported (safety)
      - Save-back fails (in-memory copy still has the field for this session)
    """
    candidates = scan.get("candidates") or []
    if not candidates:
        return

    # Sentinel: once we've attempted a backfill on a scan, tag it so we don't
    # re-run the slow path next time even when `updated == 0` (e.g. price
    # cache had no data for those tickers). Without this sentinel, legacy
    # scans whose tickers aren't in the current price cache would hit the
    # slow fallback path on every render forever.
    if scan.get("_pct50_backfill_attempted"):
        return

    # Detect: if any candidate in a small sample has the field, assume the
    # whole scan does. Avoid a full sweep to keep the fast path cheap.
    sample = candidates[:min(30, len(candidates))]
    if any(c.get("Pct_Above_50MA") is not None for c in sample):
        return  # already backfilled or produced by newer scanner

    try:
        from .data_cache import load_price_history as _load_prices  # type: ignore
    except Exception:
        return  # can't compute without the cache; UI will show "—" values

    updated = 0
    for c in candidates:
        ticker = c.get("Ticker") or ""
        if not ticker:
            continue
        try:
            df = _load_prices(ticker)
            if df is None or len(df) < 50 or "Close" not in df.columns:
                continue
            close_last = float(df["Close"].iloc[-1])
            sma50 = float(df["Close"].tail(50).mean())
            if sma50 > 0:
                c["Pct_Above_50MA"] = round((close_last - sma50) / sma50 * 100, 2)
                updated += 1
            # Backfill Pct_From_52wHigh in the same pass — same price-cache
            # load, so effectively free. Needed for the Rotation table's
            # New-Highs Breadth column (added 2026-07-10). Uses whatever
            # history is available (up to 252 trading days).
            high_52w = float(df["Close"].tail(252).max())
            if high_52w > 0:
                c["Pct_From_52wHigh"] = round((high_52w - close_last) / high_52w * 100, 2)
        except Exception:
            continue

    # Always tag the scan as "backfill attempted" — even if updated==0 —
    # so we don't try again on the next render. Without this we'd re-run
    # 6,000+ pkl reads every single time on scans whose tickers happen to
    # be missing from the current price cache.
    scan["_pct50_backfill_attempted"] = True

    # Atomic write-back — same tmp-then-replace pattern used elsewhere so
    # a crash mid-write leaves the previous scan intact.
    try:
        tmp_path = scan_path.with_suffix(scan_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(scan, indent=2, default=str),
            encoding="utf-8",
        )
        import os as _os
        _os.replace(tmp_path, scan_path)
    except Exception:
        # Failure to persist means we'll re-backfill next session. Session
        # in-memory still has the field so UI shows values for this session.
        pass


# ---------------------------------------------------------------------------
# Industry rollup
# ---------------------------------------------------------------------------

def industry_summary_from_scan(scan: dict, min_members: int = 2) -> pd.DataFrame:
    """Aggregate the scan into a per-industry summary.

    Prefers the scan\'s full ``industry_universe`` rollup (every RS-rated
    ticker with a cached industry — typically 1,500-3,000 stocks across
    60-80 industries). Falls back to just the ``candidates`` list (much
    narrower — only ~20 industries) for OLDER scans that pre-date the
    universe-rollup field.

    Output DataFrame columns:
        Industry, Sector, Members, MeanRS, PctAbove50MA
    PctAbove50MA is only populated when we have the data (i.e. from the
    candidates path); empty when using the universe rollup. The rotation
    page renders it as "—" when missing.
    """
    if not scan:
        return pd.DataFrame()

    rows = []

    # Path 1 (preferred): full-universe rollup from new scan schema.
    universe = scan.get("industry_universe") or []
    if universe:
        for u in universe:
            ind = u.get("Industry") or "N/A"
            sec = u.get("Sector") or "N/A"
            rs = u.get("RS")
            if ind == "N/A":
                continue
            rows.append({
                "Industry": ind,
                "Sector": sec,
                "RS": float(rs) if rs is not None else None,
                "Pct50": None,  # not available in the universe rollup
            })
    else:
        # Path 2 (legacy): aggregate from passing candidates only.
        candidates = scan.get("candidates", []) or []
        for c in candidates:
            ind = c.get("Industry") or "N/A"
            sec = c.get("Sector") or "N/A"
            if ind == "N/A":
                continue
            rs = c.get("RS")
            pct50 = c.get("Pct_Above_50MA")
            rows.append({
                "Industry": ind,
                "Sector": sec,
                "RS": float(rs) if rs is not None else None,
                "Pct50": float(pct50) if pct50 is not None else None,
            })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    grp = df.groupby("Industry").agg(
        Sector=("Sector", lambda s: s.mode().iloc[0] if len(s.mode()) else "N/A"),
        Members=("RS", "count"),
        MeanRS=("RS", "mean"),
        PctAbove50MA=("Pct50", "mean"),
    ).reset_index()
    grp = grp[grp["Members"] >= min_members]
    if grp.empty:
        return pd.DataFrame()
    # Defensive to_numeric — protects against object-dtype columns from
    # all-None aggregates (same fix as the Quality version below).
    grp["MeanRS"] = pd.to_numeric(grp["MeanRS"], errors="coerce").round(1)
    grp["PctAbove50MA"] = pd.to_numeric(grp["PctAbove50MA"], errors="coerce").round(1)
    return grp


def industry_summary_from_quality_scan(scan: dict, min_members: int = 2,
                                        momentum_scan: Optional[dict] = None) -> pd.DataFrame:
    """Aggregate a Quality scan into a per-industry summary.

    Quality scans cover the full Russell 3000 universe (~3,000 stocks across
    ~80 industries), which is much broader than the Momentum scan's filtered
    candidates (~150 stocks across ~20 industries). That makes industry-level
    RS aggregations much more statistically meaningful — every industry has
    real sample size, not just the handful of momentum-passing stocks.

    Uses RS_6m as the RS proxy (Quality's 6-month price return percentile —
    the closest analogue to Momentum's RS field).

    Pct_Above_50MA — new Quality scans (post-2026-07-02) populate this per
    candidate directly. Older cached scans don't have the field; for those
    we lazily fall back to loading each ticker's price pkl from data_cache
    to compute it on the fly. This keeps the "% > 50MA" column populated
    without requiring the user to run a fresh Quality scan.
    """
    if not scan:
        return pd.DataFrame()
    candidates = scan.get("candidates", []) or []
    if not candidates:
        return pd.DataFrame()

    # Detect once whether this scan is a legacy one missing Pct_Above_50MA.
    # If the first few candidates don't have the field we assume none do
    # and enable the price-cache fallback path. Avoids doing an expensive
    # sample-every-row check for every scan we handle.
    #
    # Sentinel check: if _backfill_pct_above_50ma_if_missing has already run
    # on this scan, whatever tickers COULD be resolved from the price cache
    # already have Pct_Above_50MA set — the rest never will (cache doesn't
    # have them). Skip the fallback loop entirely; 6,000 cache-miss lookups
    # per render was what caused the Rotation tab to hang for 30-60s on
    # legacy scans (2026-07-10 fix).
    sample = candidates[:min(30, len(candidates))]
    scan_has_pct50 = any(c.get("Pct_Above_50MA") is not None for c in sample)
    scan_was_backfilled = scan.get("_pct50_backfill_attempted") is True

    if not scan_has_pct50 and not scan_was_backfilled:
        # Fallback: compute Pct_Above_50MA per ticker from cached price data.
        # Loads scanner.data_cache.load_price_history for every candidate; not
        # ideal in aggregate speed but the alternative is "user must rescan
        # ~3,000 tickers on a rate-limited connection", which is worse.
        try:
            from .data_cache import load_price_history as _load_prices  # type: ignore
        except Exception:
            _load_prices = None
    else:
        _load_prices = None

    # Build a per-ticker lookup from the current Momentum scan (if provided).
    # Used for two Minervini-style enrichments:
    #   1. Setup Density — count of industry members that appear in the
    #      Momentum scan with a valid setup (VCP A/B or BUY_ZONE signal).
    #   2. The General's signal — best-RS member's current setup label
    #      (BUY ZONE, VCP A, BREAKOUT, etc.) when it's in the Momentum scan.
    #
    # No-op when momentum_scan is None (e.g. rank-comparison calls for
    # historical scans that only need MeanRS + PctAbove50MA).
    momentum_by_ticker: dict[str, dict] = {}
    if momentum_scan and momentum_scan.get("candidates"):
        for mc in momentum_scan["candidates"]:
            t = str(mc.get("Ticker") or "").upper()
            if t:
                momentum_by_ticker[t] = mc

    def _momentum_signal_for(ticker: str) -> str:
        """Return a short human-readable signal label from the Momentum scan,
        or empty string if not present. Priority order matches how a trader
        would want to know: buyable-now first, then base-quality, then just
        "on the list". BUY_ZONE > VCP grade A > breakout today > VCP B > blank."""
        mc = momentum_by_ticker.get(ticker.upper())
        if not mc:
            return ""
        signals = mc.get("Signals") or []
        if isinstance(signals, str):
            # Legacy scans stored Signals as a stringified list; parse loosely.
            signals = [s.strip(" '\"") for s in signals.strip("[]").split(",") if s.strip()]
        signals_set = set(signals) if isinstance(signals, (list, set)) else set()
        if "BUY_ZONE" in signals_set:
            return "BUY ZONE"
        if str(mc.get("VCP_Quality") or "").upper() == "A":
            return "VCP A"
        if str(mc.get("Breakout_Today") or "").lower() == "true":
            return "BREAKOUT"
        if str(mc.get("VCP_Quality") or "").upper() == "B":
            return "VCP B"
        return "on list"

    rows = []
    for c in candidates:
        ind = c.get("Industry") or "N/A"
        sec = c.get("Sector") or "N/A"
        if ind == "N/A":
            continue
        rs = c.get("RS_6m")
        ticker = c.get("Ticker") or ""
        # Prefer the field if present; otherwise compute from price cache.
        pct50 = c.get("Pct_Above_50MA")
        if pct50 is None and _load_prices is not None:
            try:
                df_prices = _load_prices(ticker)
                if df_prices is not None and len(df_prices) >= 50 and "Close" in df_prices.columns:
                    close_last = float(df_prices["Close"].iloc[-1])
                    sma50 = float(df_prices["Close"].tail(50).mean())
                    if sma50 > 0:
                        pct50 = round((close_last - sma50) / sma50 * 100, 2)
            except Exception:
                pct50 = None
        # Pct_From_52wHigh — new field for Near-Highs breadth. If missing
        # from the scan and price cache is available, compute inline.
        from52wh = c.get("Pct_From_52wHigh")
        if from52wh is None and _load_prices is not None:
            try:
                df_prices = _load_prices(ticker)
                if df_prices is not None and "Close" in df_prices.columns:
                    close_last = float(df_prices["Close"].iloc[-1])
                    high_52w = float(df_prices["Close"].tail(252).max())
                    if high_52w > 0:
                        from52wh = round((high_52w - close_last) / high_52w * 100, 2)
            except Exception:
                from52wh = None
        rows.append({
            "Industry": ind,
            "Sector": sec,
            "Ticker": ticker,
            "RS": float(rs) if rs is not None else None,
            "Pct50": float(pct50) if pct50 is not None else None,
            "From52wH": float(from52wh) if from52wh is not None else None,
            # In-Momentum flag: True if this candidate is also in the current
            # Momentum scan with any valid setup label (not just "on list").
            # That's the tightest definition of a "buyable setup right now"
            # since Momentum scans already gate by VCP/breakout/BUY_ZONE etc.
            "InSetup": ticker.upper() in momentum_by_ticker,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    # Per-industry aggregations. Two-pass approach:
    #   Pass 1: standard groupby.agg for the numeric rollups.
    #   Pass 2: iterate groups to identify "The General" (top-RS ticker) and
    #           its signal — GroupBy.agg can't easily emit ticker + signal
    #           together, so a plain loop is clearer.
    def _rs_strong_pct(s: pd.Series) -> Optional[float]:
        # % of members with RS_6m ≥ 80 — Leadership Breadth.
        # Skips None values from the count so a group of 10 with 3 rated
        # and 2 strong reports 66.7%, not 20%.
        vals = pd.to_numeric(s, errors="coerce").dropna()
        if len(vals) == 0:
            return None
        return round((vals >= 80).mean() * 100, 1)

    def _near_high_pct(s: pd.Series) -> Optional[float]:
        # % of members within 5% of their 52w high — New-Highs Breadth.
        # Minervini's "coordination" signal: many stocks in the group
        # attacking their highs together = institutional flow.
        vals = pd.to_numeric(s, errors="coerce").dropna()
        if len(vals) == 0:
            return None
        return round((vals <= 5.0).mean() * 100, 1)

    def _setup_density_pct(s: pd.Series) -> Optional[float]:
        # % of members with a valid Momentum setup right now — Setup Density.
        # 0 whenever no Momentum scan was provided (all InSetup values False).
        if len(s) == 0:
            return None
        return round(s.mean() * 100, 1)

    grp = df.groupby("Industry").agg(
        Sector=("Sector", lambda s: s.mode().iloc[0] if len(s.mode()) else "N/A"),
        Members=("RS", "count"),
        MeanRS=("RS", "mean"),
        PctAbove50MA=("Pct50", "mean"),
        RS_Strong_Pct=("RS", _rs_strong_pct),
        Near_High_Pct=("From52wH", _near_high_pct),
        Setup_Density=("InSetup", _setup_density_pct),
    ).reset_index()

    # Pass 2 — "The General": highest-RS member per industry, with signal.
    # Iterate to keep ticker + signal paired; a groupby.transform + merge
    # would also work but is less readable here.
    general_map: dict[str, tuple[str, Optional[float], str]] = {}
    for ind, sub in df.groupby("Industry"):
        rs_sorted = sub.dropna(subset=["RS"]).sort_values("RS", ascending=False)
        if rs_sorted.empty:
            general_map[ind] = ("", None, "")
            continue
        top = rs_sorted.iloc[0]
        top_ticker = str(top["Ticker"])
        general_map[ind] = (
            top_ticker,
            float(top["RS"]) if top["RS"] is not None else None,
            _momentum_signal_for(top_ticker),
        )
    grp["General_Ticker"] = grp["Industry"].map(lambda i: general_map.get(i, ("", None, ""))[0])
    grp["General_RS"]     = grp["Industry"].map(lambda i: general_map.get(i, ("", None, ""))[1])
    grp["General_Signal"] = grp["Industry"].map(lambda i: general_map.get(i, ("", None, ""))[2])

    grp = grp[grp["Members"] >= min_members]
    if grp.empty:
        return pd.DataFrame()
    # Use pd.to_numeric so columns that are all None (e.g. PctAbove50MA from
    # Quality scans, which don't store this field) coerce cleanly to NaN
    # instead of raising "Expected numeric dtype, got object instead."
    grp["MeanRS"] = pd.to_numeric(grp["MeanRS"], errors="coerce").round(1)
    grp["PctAbove50MA"] = pd.to_numeric(grp["PctAbove50MA"], errors="coerce").round(1)
    grp["RS_Strong_Pct"] = pd.to_numeric(grp["RS_Strong_Pct"], errors="coerce").round(1)
    grp["Near_High_Pct"] = pd.to_numeric(grp["Near_High_Pct"], errors="coerce").round(1)
    grp["Setup_Density"] = pd.to_numeric(grp["Setup_Density"], errors="coerce").round(1)
    grp["General_RS"] = pd.to_numeric(grp["General_RS"], errors="coerce").round(0)
    return grp


def _rank_industries_df(df: pd.DataFrame) -> pd.DataFrame:
    """Assign Rank (1 = best) by MeanRS descending."""
    if df.empty:
        return df
    df = df.sort_values("MeanRS", ascending=False).reset_index(drop=True)
    df["Rank"] = df.index + 1
    return df


# ---------------------------------------------------------------------------
# Main rotation table
# ---------------------------------------------------------------------------

def compute_rotation_table(
    latest_scan: Optional[dict] = None,
    window_days_short: int = 7,
    window_days_long: int = 28,
    tolerance_days: float = 2.5,
) -> dict:
    """Build the industry-rotation DataFrame.

    Source priority:
      1. Quality scans (preferred) — full Russell 3000 universe (~3,000 stocks,
         ~80 industries) makes industry RS aggregations statistically meaningful.
         RS_6m is the per-stock RS field.
      2. Momentum scans (fallback) — only used when no Quality scans exist
         on disk. Uses the legacy RS field.

    Returns a dict with:
        df (pd.DataFrame): per-industry rows including Minervini-style leadership
            signals (RS_Strong_Pct, Near_High_Pct, Setup_Density, General_*).
        history (dict): {"short": timestamp_str_or_None, "long": same}
        latest_timestamp (str): timestamp of the scan used for the current row
        source (str): "quality" or "momentum"
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": None,
        "source": "none",
    }

    # ---- Prefer Quality scans ----
    quality_scans = list_quality_scans()
    if quality_scans:
        latest_ts, latest_path = quality_scans[0]
        latest_q = load_scan_json(latest_path)
        if latest_q is None:
            return out
        # One-time backfill of Pct_Above_50MA + Pct_From_52wHigh for scans
        # predating those fields. Without this, industry_summary_from_quality_scan
        # runs its slow price-cache fallback path on EVERY render, loading
        # ~3,000 price pkls per rerun.
        _backfill_pct_above_50ma_if_missing(latest_q, latest_path)
        return _compute_from_scans(
            latest_scan=latest_q,
            latest_ts=latest_ts,
            latest_path=latest_path,
            summary_fn=industry_summary_from_quality_scan,
            find_near_fn=find_quality_scan_near,
            window_days_short=window_days_short,
            window_days_long=window_days_long,
            tolerance_days=tolerance_days,
            source_label="quality",
            momentum_scan=latest_scan,  # for Setup Density + The General
        )

    # ---- Fallback: Momentum scans ----
    if not latest_scan:
        return out
    ts_str = latest_scan.get("timestamp") or ""
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        latest_dt = datetime.fromisoformat(ts_str)
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except Exception:
        latest_dt = datetime.now(timezone.utc)
    latest_path_candidates = [p for ts, p in list_long_scans() if ts == latest_dt]
    latest_path = latest_path_candidates[0] if latest_path_candidates else None
    return _compute_from_scans(
        latest_scan=latest_scan,
        latest_ts=latest_dt,
        latest_path=latest_path,
        summary_fn=industry_summary_from_scan,
        find_near_fn=find_scan_near,
        window_days_short=window_days_short,
        window_days_long=window_days_long,
        tolerance_days=tolerance_days,
        source_label="momentum",
    )


def _compute_from_scans(
    latest_scan: dict,
    latest_ts: datetime,
    latest_path: Optional[Path],
    summary_fn,
    find_near_fn,
    window_days_short: int,
    window_days_long: int,
    tolerance_days: float,
    source_label: str,
    momentum_scan: Optional[dict] = None,
) -> dict:
    """Shared rotation-table computation. Parameterized by data source.

    momentum_scan: optional Momentum scan passed to summary_fn for the CURRENT
    row's rollup only (enables Setup Density + General signal enrichment).
    Historical rank-comparison summaries don't need it — they only compute
    MeanRS for the rank map.
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": latest_scan.get("timestamp"),
        "source": source_label,
    }

    # summary_fn for quality accepts momentum_scan kwarg; momentum-fallback
    # doesn't. Branch by source_label.
    if source_label == "quality":
        cur_df = _rank_industries_df(summary_fn(latest_scan, momentum_scan=momentum_scan))
    else:
        cur_df = _rank_industries_df(summary_fn(latest_scan))
    if cur_df.empty:
        out["df"] = cur_df
        return out

    short_target = latest_ts - timedelta(days=window_days_short)
    long_target  = latest_ts - timedelta(days=window_days_long)

    short_path = find_near_fn(short_target, tolerance_days=tolerance_days)
    long_path  = find_near_fn(long_target,  tolerance_days=tolerance_days)

    # Don't compare a scan to itself (happens when history is sparse)
    if short_path is not None and latest_path is not None and short_path == latest_path:
        short_path = None
    if long_path is not None and latest_path is not None and long_path == latest_path:
        long_path = None

    short_rank_map: dict[str, int] = {}
    long_rank_map: dict[str, int] = {}

    # Backfill Pct_Above_50MA on historical scans too — not just the latest.
    # Without this, a legacy scan loaded for rank comparison triggers the slow
    # price-cache fallback in industry_summary_from_quality_scan on EVERY
    # render (6,000+ pkl reads per render). Only applies to Quality path.
    _do_backfill = source_label == "quality"

    if short_path is not None:
        short_scan = load_scan_json(short_path)
        if short_scan:
            if _do_backfill:
                _backfill_pct_above_50ma_if_missing(short_scan, short_path)
            sdf = _rank_industries_df(summary_fn(short_scan))
            short_rank_map = dict(zip(sdf["Industry"], sdf["Rank"]))
            out["history"]["short"] = short_scan.get("timestamp")

    if long_path is not None:
        long_scan = load_scan_json(long_path)
        if long_scan:
            if _do_backfill:
                _backfill_pct_above_50ma_if_missing(long_scan, long_path)
            ldf = _rank_industries_df(summary_fn(long_scan))
            long_rank_map = dict(zip(ldf["Industry"], ldf["Rank"]))
            out["history"]["long"] = long_scan.get("timestamp")

    cur_df["Rank_1w"] = cur_df["Industry"].map(short_rank_map)
    cur_df["Rank_4w"] = cur_df["Industry"].map(long_rank_map)

    def _delta(row, col):
        prior = row.get(col)
        if prior is None or pd.isna(prior):
            return None
        return int(prior) - int(row["Rank"])  # positive = climbed

    cur_df["Delta_1w"] = cur_df.apply(lambda r: _delta(r, "Rank_1w"), axis=1)
    cur_df["Delta_4w"] = cur_df.apply(lambda r: _delta(r, "Rank_4w"), axis=1)

    # Rotation score: weighted blend of rank-changes + current absolute RS.
    def _score(row):
        rs = row.get("MeanRS") or 0
        d4 = row.get("Delta_4w")
        d1 = row.get("Delta_1w")
        score = float(rs)
        if d4 is not None and not pd.isna(d4):
            score += 1.5 * float(d4)
        if d1 is not None and not pd.isna(d1):
            score += 0.75 * float(d1)
        return round(score, 1)

    cur_df["RotationScore"] = cur_df.apply(_score, axis=1)

    cols = [
        "Industry", "Sector", "Members", "Rank",
        "Rank_1w", "Rank_4w", "Delta_1w", "Delta_4w",
        "MeanRS", "PctAbove50MA",
        # Minervini-style leadership signals (added 2026-07-10):
        "RS_Strong_Pct",     # Leadership Breadth — % members with RS_6m ≥ 80
        "Near_High_Pct",     # New-Highs Breadth — % members within 5% of 52wH
        "Setup_Density",     # % members with a Momentum-scan setup right now
        "General_Ticker",    # Top-RS "canary" stock in the group
        "General_RS",        # Its RS_6m
        "General_Signal",    # Its current Momentum-scan signal (if any)
        "RotationScore",
    ]
    cur_df = cur_df[[c for c in cols if c in cur_df.columns]]
    out["df"] = cur_df.reset_index(drop=True)
    return out


def top_rotators(rotation_df: pd.DataFrame, n: int = 6) -> pd.DataFrame:
    """Return the top-N industries by RotationScore — used by the Layer 2
    summary strip to show "what else is hot" peripheral context."""
    if rotation_df is None or rotation_df.empty:
        return pd.DataFrame()
    sorted_df = rotation_df.sort_values("RotationScore", ascending=False, na_position="last")
    return sorted_df.head(n).reset_index(drop=True)

# Main rotation table
# ---------------------------------------------------------------------------

def compute_rotation_table(
    latest_scan: Optional[dict] = None,
    window_days_short: int = 7,
    window_days_long: int = 28,
    tolerance_days: float = 2.5,
) -> dict:
    """Build the industry-rotation DataFrame.

    Source priority (2026-06-17 update — was previously Momentum-only):
      1. Quality scans (preferred) — full Russell 3000 universe (~3,000 stocks,
         ~80 industries) makes industry RS aggregations statistically meaningful.
         The user typically has many more Quality scans than Momentum scans,
         so history is available immediately. RS_6m is the per-stock RS field.
      2. Momentum scans (fallback) — only used when no Quality scans exist
         on disk (early-stage users). Uses the legacy RS field.

    The `latest_scan` argument is kept for backward compatibility but is only
    consulted in the Momentum fallback path. With Quality available, this
    function loads its own latest Quality scan from disk.

    Returns a dict with:
        df (pd.DataFrame): per-industry rows (Industry, Sector, Members, Rank,
            Rank_1w, Rank_4w, Delta_1w, Delta_4w, MeanRS, PctAbove50MA,
            RotationScore)
        history (dict): {"short": timestamp_str_or_None, "long": same}
        latest_timestamp (str): timestamp of the scan used for the current row
        source (str): "quality" or "momentum"

    Delta_* values are POSITIVE when an industry CLIMBS in rank (e.g. went from
    rank 30 to rank 5 → Delta = +25). Missing history leaves Rank_* / Delta_*
    as None / NaN; UI renders these as "—".
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": None,
        "source": "none",
    }

    # ---- Prefer Quality scans ----
    quality_scans = list_quality_scans()
    if quality_scans:
        latest_ts, latest_path = quality_scans[0]
        latest_q = load_scan_json(latest_path)
        if latest_q is None:
            return out
        # One-time backfill of Pct_Above_50MA + Pct_From_52wHigh for scans
        # predating those fields. Without this, industry_summary_from_quality_scan
        # runs its slow price-cache fallback path on EVERY render, loading
        # ~3,000 price pkls per rerun. This helper computes the fields once
        # and writes back to disk so all subsequent loads (this session AND
        # future sessions) find the fields present and skip the fallback.
        _backfill_pct_above_50ma_if_missing(latest_q, latest_path)
        return _compute_from_scans(
            latest_scan=latest_q,
            latest_ts=latest_ts,
            latest_path=latest_path,
            summary_fn=industry_summary_from_quality_scan,
            find_near_fn=find_quality_scan_near,
            window_days_short=window_days_short,
            window_days_long=window_days_long,
            tolerance_days=tolerance_days,
            source_label="quality",
            momentum_scan=latest_scan,  # used only for latest-row enrichment
        )

    # ---- Fallback: Momentum scans (original behavior) ----
    if not latest_scan:
        return out
    ts_str = latest_scan.get("timestamp") or ""
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        latest_dt = datetime.fromisoformat(ts_str)
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except Exception:
        latest_dt = datetime.now(timezone.utc)
    latest_path_candidates = [p for ts, p in list_long_scans() if ts == latest_dt]
    latest_path = latest_path_candidates[0] if latest_path_candidates else None
    return _compute_from_scans(
        latest_scan=latest_scan,
        latest_ts=latest_dt,
        latest_path=latest_path,
        summary_fn=industry_summary_from_scan,
        find_near_fn=find_scan_near,
        window_days_short=window_days_short,
        window_days_long=window_days_long,
        tolerance_days=tolerance_days,
        source_label="momentum",
    )


def _compute_from_scans(
    latest_scan: dict,
    latest_ts: datetime,
    latest_path: Optional[Path],
    summary_fn,
    find_near_fn,
    window_days_short: int,
    window_days_long: int,
    tolerance_days: float,
    source_label: str,
    momentum_scan: Optional[dict] = None,
) -> dict:
    """Shared rotation-table computation. Parameterized by data source.

    summary_fn: industry_summary_from_scan OR industry_summary_from_quality_scan
    find_near_fn: find_scan_near OR find_quality_scan_near
    momentum_scan: optional Momentum scan, passed through to summary_fn for
        the CURRENT-row rollup only. Enables Setup Density + General Signal
        enrichment. Historical rank-comparison summaries don't get it —
        they only need MeanRS for the rank map.
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": latest_scan.get("timestamp"),
        "source": source_label,
    }

    # Pass momentum_scan through only when summary_fn is the quality variant;
    # the momentum-fallback summary_fn doesn't accept the kwarg.
    if source_label == "quality":
        cur_df = _rank_industries_df(summary_fn(latest_scan, momentum_scan=momentum_scan))
    else:
        cur_df = _rank_industries_df(summary_fn(latest_scan))
    if cur_df.empty:
        out["df"] = cur_df
        return out

    short_target = latest_ts - timedelta(days=window_days_short)
    long_target  = latest_ts - timedelta(days=window_days_long)

    short_path = find_near_fn(short_target, tolerance_days=tolerance_days)
    long_path  = find_near_fn(long_target,  tolerance_days=tolerance_days)

    # Don't compare a scan to itself (happens when history is sparse)
    if short_path is not None and latest_path is not None and short_path == latest_path:
        short_path = None
    if long_path is not None and latest_path is not None and long_path == latest_path:
        long_path = None

    short_rank_map: dict[str, int] = {}
    long_rank_map: dict[str, int] = {}

    # Backfill Pct_Above_50MA on historical scans too — not just the
    # latest one. Without this, a legacy scan (pre-2026-07-02) loaded for
    # rank comparison triggers the slow price-cache fallback in
    # industry_summary_from_quality_scan on EVERY render, loading 6,000+
    # price pkls each time. On Windows this locks the UI for 30-60s and
    # the user sees "loading forever" (reported 2026-07-10). Only applies
    # to the Quality path; Momentum scans don't use this field.
    _do_backfill = source_label == "quality"

    if short_path is not None:
        short_scan = load_scan_json(short_path)
        if short_scan:
            if _do_backfill:
                _backfill_pct_above_50ma_if_missing(short_scan, short_path)
            sdf = _rank_industries_df(summary_fn(short_scan))
            short_rank_map = dict(zip(sdf["Industry"], sdf["Rank"]))
            out["history"]["short"] = short_scan.get("timestamp")

    if long_path is not None:
        long_scan = load_scan_json(long_path)
        if long_scan:
            if _do_backfill:
                _backfill_pct_above_50ma_if_missing(long_scan, long_path)
            ldf = _rank_industries_df(summary_fn(long_scan))
            long_rank_map = dict(zip(ldf["Industry"], ldf["Rank"]))
            out["history"]["long"] = long_scan.get("timestamp")

    cur_df["Rank_1w"] = cur_df["Industry"].map(short_rank_map)
    cur_df["Rank_4w"] = cur_df["Industry"].map(long_rank_map)

    def _delta(row, col):
        prior = row.get(col)
        if prior is None or pd.isna(prior):
            return None
        return int(prior) - int(row["Rank"])  # positive = climbed

    cur_df["Delta_1w"] = cur_df.apply(lambda r: _delta(r, "Rank_1w"), axis=1)
    cur_df["Delta_4w"] = cur_df.apply(lambda r: _delta(r, "Rank_4w"), axis=1)

    # Rotation score: weighted blend of rank-changes + current absolute RS.
    # 4w change weighted highest (the real rotation signal), then 1w, then
    # current RS as tie-breaker. Industries with no history fall back to RS only.
    def _score(row):
        rs = row.get("MeanRS") or 0
        d4 = row.get("Delta_4w")
        d1 = row.get("Delta_1w")
        score = float(rs)
        if d4 is not None and not pd.isna(d4):
            score += 1.5 * float(d4)
        if d1 is not None and not pd.isna(d1):
            score += 0.75 * float(d1)
        return round(score, 1)

    cur_df["RotationScore"] = cur_df.apply(_score, axis=1)

    cols = [
        "Industry", "Sector", "Members", "Rank",
        "Rank_1w", "Rank_4w", "Delta_1w", "Delta_4w",
        "MeanRS", "PctAbove50MA",
        # Minervini-style leadership signals (added 2026-07-10):
        "RS_Strong_Pct",     # Leadership Breadth — % members with RS_6m ≥ 80
        "Near_High_Pct",     # New-Highs Breadth — % members within 5% of 52wH
        "Setup_Density",     # % members with a Momentum-scan setup right now
        "General_Ticker",    # Top-RS "canary" stock in the group
        "General_RS",        # Its RS_6m
        "General_Signal",    # Its current Momentum-scan signal (if any)
        "RotationScore",
    ]
    cur_df = cur_df[[c for c in cols if c in cur_df.columns]]
    out["df"] = cur_df.reset_index(drop=True)
    return out


def top_rotators(rotation_df: pd.DataFrame, n: int = 6) -> pd.DataFrame:
    """Return the top-N industries by RotationScore — used by the Layer 2
    summary strip to show "what else is hot" peripheral context."""
    if rotation_df is None or rotation_df.empty:
        return pd.DataFrame()
    sorted_df = rotation_df.sort_values("RotationScore", ascending=False, na_position="last")
    return sorted_df.head(n).reset_index(drop=True)
