"""
Incremental data cache for scanner inputs.

Two caches, both rooted at ``data/cache/`` next to the existing scan-results
cache:

  - ``data/cache/fundamentals.json`` — per-ticker dict of fundamentals
    fields, each with a ``cached_at`` ISO timestamp. Fields are grouped
    into three TTL buckets:
        * static   (sector, industry)            — 90 days
        * mktcap   (mktcap_raw, shares_outstanding) — 30 days
        * growth   (peg, eps_flag, eps_accel, rev_flag, earnings_date) — 5 days
    The reason for grouping: each group corresponds to one yfinance call
    (info / quarterly_financials / calendar). We only re-issue a call when
    that group is stale, not when ANY field is stale.

  - ``data/cache/prices/<TICKER>.pkl`` — per-ticker pandas DataFrame
    (pickled via DataFrame.to_pickle) of OHLCV bars. Pickle keeps the
    DateTimeIndex intact and avoids adding pyarrow as a hard dependency.

Safety net contract:
  * Every read function MUST swallow exceptions and return a cache miss
    sentinel (None or {}) on any failure. A corrupt cache file should
    never crash the scan; it should just trigger a fresh fetch.
  * Every write function should be best-effort: if disk is full or the
    file can't be written, log and continue. Cache writes are a perf
    optimisation, not a correctness requirement.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Cache locations
# ---------------------------------------------------------------------------

# Resolve relative to the repo root (parent of this scanner/ package).
_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "data" / "cache"
PRICES_DIR = CACHE_DIR / "prices"
FUNDAMENTALS_PATH = CACHE_DIR / "fundamentals.json"


def _ensure_dirs():
    """Create cache directories on demand. Safe to call repeatedly."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PRICES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # If we can't create the cache dirs, fall through. Reads will miss
        # and writes will fail silently — scan still works, just uncached.
        pass


# ---------------------------------------------------------------------------
# TTL helpers
# ---------------------------------------------------------------------------

# Group -> max age in days. Smaller = fresher data, more cache misses.
TTL_DAYS = {
    "static":    90,   # sector, industry — these almost never change
    "mktcap":    30,   # shares_outstanding — only changes on buyback/offering/split
    "growth":     5,   # PEG, EPS flags, earnings date — analyst-driven
    "estimates":  7,   # forward EPS/sales/growth — analysts revise on earnings
    "quality":   30,   # ROE, margins, debt, cashflow, historical EPS/Sales — quarterly filings
    "ownership": 14,   # institutional %, mutual %, short interest — bi-monthly + 13F cadence
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO timestamp, tolerant of missing tz / trailing Z."""
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def age_days(cached_at: Optional[str]) -> Optional[float]:
    """Return age of a cached_at timestamp in days, or None if unparseable."""
    dt = _parse_iso(cached_at)
    if dt is None:
        return None
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 86400.0


def is_fresh(cached_at: Optional[str], group: str) -> bool:
    """True iff the entry was cached within the group's TTL window."""
    ttl = TTL_DAYS.get(group)
    if ttl is None:
        return False
    age = age_days(cached_at)
    if age is None:
        return False
    return age <= ttl


def cache_age_label(cached_at: Optional[str]) -> str:
    """Human-readable age, suitable for a small UI caption.
    Returns '' if no timestamp available."""
    age = age_days(cached_at)
    if age is None:
        return ""
    if age < 1 / 24:  # under an hour
        mins = int(age * 24 * 60)
        return f"cached {mins}m ago"
    if age < 1:
        hrs = int(age * 24)
        return f"cached {hrs}h ago"
    return f"cached {int(age)}d ago"


# ---------------------------------------------------------------------------
# Fundamentals cache
# ---------------------------------------------------------------------------

def load_fundamentals_cache() -> dict:
    """Read the full fundamentals JSON. Returns {} on any error."""
    try:
        if not FUNDAMENTALS_PATH.exists():
            return {}
        with FUNDAMENTALS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        # Corrupt JSON? Treat as cold cache. Don't crash the scan.
        return {}


def save_fundamentals_cache(cache: dict) -> bool:
    """Write the full fundamentals JSON. Returns True on success, False otherwise."""
    _ensure_dirs()
    try:
        tmp_path = FUNDAMENTALS_PATH.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=False, default=str)
        # Atomic-ish rename so a partial write never overwrites a good cache
        tmp_path.replace(FUNDAMENTALS_PATH)
        return True
    except Exception:
        return False


def get_cached_fundamentals(ticker: str, cache: Optional[dict] = None) -> dict:
    """Return the cached entry for a ticker, or {} if not present.
    Always returns a dict so callers can do .get(field) without surprise."""
    if cache is None:
        cache = load_fundamentals_cache()
    entry = cache.get(ticker)
    if not isinstance(entry, dict):
        return {}
    return entry


def need_refresh(entry: dict, group: str) -> bool:
    """True iff the entry's group-specific cached_at field is missing or stale."""
    ts_key = f"{group}_cached_at"
    return not is_fresh(entry.get(ts_key), group)


def update_fundamentals_entry(
    cache: dict,
    ticker: str,
    *,
    static: Optional[dict] = None,
    mktcap: Optional[dict] = None,
    growth: Optional[dict] = None,
    estimates: Optional[dict] = None,
    quality: Optional[dict] = None,
    ownership: Optional[dict] = None,
) -> dict:
    """Merge a fetch result back into the cache entry for one ticker.

    Each group is optional — pass only the groups you actually refreshed.
    Stamps the appropriate ``<group>_cached_at`` timestamps. Mutates
    ``cache`` in place AND returns the updated entry for convenience.

    Groups:
      static     — sector, industry (rarely changes)
      mktcap     — shares_outstanding (changes on buyback/offering/split)
      growth     — PEG, EPS flags, earnings_date (analyst-driven)
      estimates  — forward EPS/sales/growth for next 1-2 years
      quality    — ROE, margins, debt/equity, cashflow, hist EPS+Sales table
      ownership  — institutional %, mutual fund %, short interest, days to cover
    """
    entry = cache.get(ticker)
    if not isinstance(entry, dict):
        entry = {}
    now = _now_iso()
    if static is not None:
        entry.update(static)
        entry["static_cached_at"] = now
    if mktcap is not None:
        entry.update(mktcap)
        entry["mktcap_cached_at"] = now
    if growth is not None:
        entry.update(growth)
        entry["growth_cached_at"] = now
    if estimates is not None:
        entry.update(estimates)
        entry["estimates_cached_at"] = now
    if quality is not None:
        entry.update(quality)
        entry["quality_cached_at"] = now
    if ownership is not None:
        entry.update(ownership)
        entry["ownership_cached_at"] = now
    cache[ticker] = entry
    return entry


# ---------------------------------------------------------------------------
# Price history cache (per-ticker pickle files)
# ---------------------------------------------------------------------------

def _price_path(ticker: str) -> Path:
    # Sanitize ticker for filename — strip any path separators just in case
    safe = "".join(c for c in ticker if c.isalnum() or c in ("-", "_", "."))
    return PRICES_DIR / f"{safe}.pkl"


def load_price_history(ticker: str) -> Optional[pd.DataFrame]:
    """Load cached price DataFrame for a ticker. Returns None on any failure."""
    path = _price_path(ticker)
    try:
        if not path.exists():
            return None
        df = pd.read_pickle(path)
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        # Sanity: must have Close + Volume columns we depend on
        if "Close" not in df.columns or "Volume" not in df.columns:
            return None
        return df
    except Exception:
        return None


def save_price_history(ticker: str, df: pd.DataFrame) -> bool:
    """Persist a price DataFrame. Best-effort; returns True on success."""
    if df is None or df.empty:
        return False
    _ensure_dirs()
    try:
        path = _price_path(ticker)
        tmp_path = path.with_suffix(".pkl.tmp")
        df.to_pickle(tmp_path)
        tmp_path.replace(path)
        return True
    except Exception:
        return False


def get_cached_last_date(ticker: str) -> Optional[pd.Timestamp]:
    """Most recent date in the cached price history, or None if no cache."""
    df = load_price_history(ticker)
    if df is None or df.empty:
        return None
    try:
        return df.index.max()
    except Exception:
        return None


def looks_like_split(
    cached_df: pd.DataFrame,
    fresh_df: pd.DataFrame,
    tolerance: float = 0.05,
) -> bool:
    """Detect a probable stock split by comparing overlapping-day adjusted
    closes. Returns True if the cached vs. fresh close for the same date
    differs by more than ``tolerance`` (default 5%). When True, the caller
    should re-download the FULL history for this ticker so all adjusted
    prices stay internally consistent.
    """
    if cached_df is None or fresh_df is None or cached_df.empty or fresh_df.empty:
        return False
    try:
        # Find overlapping dates and compare the LATEST overlapping close
        cached_dates = set(cached_df.index)
        fresh_dates = set(fresh_df.index)
        overlap = sorted(cached_dates & fresh_dates)
        if not overlap:
            return False
        last_overlap = overlap[-1]
        c_close = float(cached_df.loc[last_overlap, "Close"])
        f_close = float(fresh_df.loc[last_overlap, "Close"])
        if c_close <= 0:
            return False
        ratio_diff = abs(f_close - c_close) / c_close
        return ratio_diff > tolerance
    except Exception:
        # If anything goes wrong, err on the side of caution — pretend no
        # split. The caller will use the cached data, which is at worst
        # one day stale rather than wholly wrong.
        return False


def merge_price_history(
    cached_df: pd.DataFrame,
    fresh_df: pd.DataFrame,
) -> pd.DataFrame:
    """Combine cached + fresh DataFrames, preferring fresh values on overlap.
    Used in the incremental-update path. Robust to either being empty."""
    if cached_df is None or cached_df.empty:
        return fresh_df
    if fresh_df is None or fresh_df.empty:
        return cached_df
    try:
        combined = pd.concat([cached_df, fresh_df])
        # Drop duplicate dates, keeping the LAST (fresh) value
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
        return combined
    except Exception:
        # Fall back to the cached version if merging blows up — better
        # than returning empty.
        return cached_df


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def cache_stats() -> dict:
    """Return a small dict describing what's in the cache. Useful for UI
    diagnostics and debugging. All fields are optional / best-effort."""
    stats = {
        "cache_dir": str(CACHE_DIR),
        "fundamentals_path": str(FUNDAMENTALS_PATH),
        "fundamentals_exists": FUNDAMENTALS_PATH.exists(),
        "n_price_files": 0,
        "n_fundamentals_tickers": 0,
        "fundamentals_size_kb": 0,
    }
    try:
        if PRICES_DIR.exists():
            stats["n_price_files"] = sum(1 for _ in PRICES_DIR.glob("*.pkl"))
        if FUNDAMENTALS_PATH.exists():
            stats["fundamentals_size_kb"] = round(FUNDAMENTALS_PATH.stat().st_size / 1024, 1)
            cache = load_fundamentals_cache()
            stats["n_fundamentals_tickers"] = len(cache)
    except Exception:
        pass
    return stats
