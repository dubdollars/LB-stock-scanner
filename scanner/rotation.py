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


def industry_summary_from_quality_scan(scan: dict, min_members: int = 2) -> pd.DataFrame:
    """Aggregate a Quality scan into a per-industry summary.

    Quality scans cover the full Russell 3000 universe (~3,000 stocks across
    ~80 industries), which is much broader than the Momentum scan's filtered
    candidates (~150 stocks across ~20 industries). That makes industry-level
    RS aggregations much more statistically meaningful — every industry has
    real sample size, not just the handful of momentum-passing stocks.

    Uses RS_6m as the RS proxy (Quality's 6-month price return percentile —
    the closest analogue to Momentum's RS field). PctAbove50MA isn't stored
    in Quality scans, so it comes back as None and the UI renders it as '—'.
    """
    if not scan:
        return pd.DataFrame()
    candidates = scan.get("candidates", []) or []
    if not candidates:
        return pd.DataFrame()
    rows = []
    for c in candidates:
        ind = c.get("Industry") or "N/A"
        sec = c.get("Sector") or "N/A"
        if ind == "N/A":
            continue
        rs = c.get("RS_6m")
        rows.append({
            "Industry": ind,
            "Sector": sec,
            "RS": float(rs) if rs is not None else None,
            "Pct50": None,  # not stored in Quality scans
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
    # Use pd.to_numeric so columns that are all None (e.g. PctAbove50MA from
    # Quality scans, which don't store this field) coerce cleanly to NaN
    # instead of raising "Expected numeric dtype, got object instead."
    grp["MeanRS"] = pd.to_numeric(grp["MeanRS"], errors="coerce").round(1)
    grp["PctAbove50MA"] = pd.to_numeric(grp["PctAbove50MA"], errors="coerce").round(1)
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
) -> dict:
    """Shared rotation-table computation. Parameterized by data source.

    summary_fn: industry_summary_from_scan OR industry_summary_from_quality_scan
    find_near_fn: find_scan_near OR find_quality_scan_near
    """
    out = {
        "df": pd.DataFrame(),
        "history": {"short": None, "long": None},
        "latest_timestamp": latest_scan.get("timestamp"),
        "source": source_label,
    }

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

    if short_path is not None:
        short_scan = load_scan_json(short_path)
        if short_scan:
            sdf = _rank_industries_df(summary_fn(short_scan))
            short_rank_map = dict(zip(sdf["Industry"], sdf["Rank"]))
            out["history"]["short"] = short_scan.get("timestamp")

    if long_path is not None:
        long_scan = load_scan_json(long_path)
        if long_scan:
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
