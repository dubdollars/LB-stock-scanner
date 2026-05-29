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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


_REPO_ROOT = Path(__file__).resolve().parent.parent
SCANS_DIR = _REPO_ROOT / "data" / "scans"


# ---------------------------------------------------------------------------
# Scan-file loading
# ---------------------------------------------------------------------------

def _scan_timestamp(path: Path) -> Optional[datetime]:
    """Parse the timestamp out of a scan filename like
    'scan_20260523_145738.json' -> datetime(2026,5,23,14,57,38, tzinfo=utc)."""
    try:
        name = path.stem  # scan_20260523_145738
        # Skip short-scan files (named scan_short_*)
        if "short_" in name:
            return None
        parts = name.split("_")
        # ["scan", "20260523", "145738"]
        date_str = parts[1]
        time_str = parts[2] if len(parts) > 2 else "000000"
        dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def list_long_scans() -> list[tuple[datetime, Path]]:
    """Return (timestamp, path) tuples for every long scan on disk, newest first."""
    out = []
    if not SCANS_DIR.exists():
        return out
    for path in SCANS_DIR.glob("scan_*.json"):
        ts = _scan_timestamp(path)
        if ts is None:
            continue
        out.append((ts, path))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def find_scan_near(target_dt: datetime, tolerance_days: float = 2.5) -> Optional[Path]:
    """Find the cached long scan closest to `target_dt` (within tolerance).
    Returns None if no scan falls within the tolerance window."""
    scans = list_long_scans()
    if not scans:
        return None
    best = None
    best_diff = None
    for ts, path in scans:
        diff = abs((ts - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best = path
            best_diff = diff
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
    grp["MeanRS"] = grp["MeanRS"].round(1)
    grp["PctAbove50MA"] = grp["PctAbove50MA"].round(1)
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
    latest_scan: dict,
    window_days_short: int = 7,
    window_days_long: int = 28,
    tolerance_days: float = 2.5,
) -> dict:
    """Build the industry-rotation DataFrame.

    Returns a dict with:
        df (pd.DataFrame): one row per industry with columns:
            Industry, Sector, Members, Rank, Rank_1w, Rank_4w,
            Delta_1w, Delta_4w, MeanRS, PctAbove50MA, RotationScore
        history (dict): which historical scan timestamps were used (None if missing)
        latest_timestamp (str): timestamp from latest_scan

    Delta_*  values are POSITIVE when an industry climbs in rank (e.g. went
    from rank 30 to rank 5 → Delta = +25). Negative when it falls.

    Missing history (no scan from ~7 days ago, for instance) leaves the
    corresponding Rank_* / Delta_* columns as None / NaN.
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": latest_scan.get("timestamp") if latest_scan else None,
    }
    if not latest_scan:
        return out

    cur_df = industry_summary_from_scan(latest_scan)
    cur_df = _rank_industries_df(cur_df)
    if cur_df.empty:
        out["df"] = cur_df
        return out

    # Parse latest scan's own timestamp so we can locate "1 week ago" relative to IT
    ts_str = latest_scan.get("timestamp") or ""
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        latest_dt = datetime.fromisoformat(ts_str)
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except Exception:
        latest_dt = datetime.now(timezone.utc)

    short_target = latest_dt - timedelta(days=window_days_short)
    long_target = latest_dt - timedelta(days=window_days_long)

    short_path = find_scan_near(short_target, tolerance_days=tolerance_days)
    long_path = find_scan_near(long_target, tolerance_days=tolerance_days)

    # Important: if the closest scan to "short_target" is actually the SAME
    # file as latest_scan (which happens when the user has only a handful of
    # scans), treat that as "no history available" rather than comparing the
    # scan to itself (which would give 0 rank change for everything).
    latest_path_candidates = [p for ts, p in list_long_scans() if ts == latest_dt]
    latest_path = latest_path_candidates[0] if latest_path_candidates else None
    if short_path is not None and latest_path is not None and short_path == latest_path:
        short_path = None
    if long_path is not None and latest_path is not None and long_path == latest_path:
        long_path = None

    short_rank_map: dict[str, int] = {}
    long_rank_map: dict[str, int] = {}

    if short_path is not None:
        short_scan = load_scan_json(short_path)
        if short_scan:
            sdf = _rank_industries_df(industry_summary_from_scan(short_scan))
            short_rank_map = dict(zip(sdf["Industry"], sdf["Rank"]))
            out["history"]["short"] = short_scan.get("timestamp")

    if long_path is not None:
        long_scan = load_scan_json(long_path)
        if long_scan:
            ldf = _rank_industries_df(industry_summary_from_scan(long_scan))
            long_rank_map = dict(zip(ldf["Industry"], ldf["Rank"]))
            out["history"]["long"] = long_scan.get("timestamp")

    # Merge history ranks back onto the current table
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
    # Heavier weight on 4w change (the real rotation signal), then 1w, then
    # current RS as a tie-breaker. Industries with no history fall back to
    # RS-only ranking so they still get scored.
    def _score(row):
        rs = row.get("MeanRS") or 0
        d4 = row.get("Delta_4w")
        d1 = row.get("Delta_1w")
        score = float(rs)  # baseline 0-99
        if d4 is not None and not pd.isna(d4):
            score += 1.5 * float(d4)
        if d1 is not None and not pd.isna(d1):
            score += 0.75 * float(d1)
        return round(score, 1)

    cur_df["RotationScore"] = cur_df.apply(_score, axis=1)

    # Final column order
    cols = [
        "Industry", "Sector", "Members", "Rank",
        "Rank_1w", "Rank_4w", "Delta_1w", "Delta_4w",
        "MeanRS", "PctAbove50MA", "RotationScore",
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
