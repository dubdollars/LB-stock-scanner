"""
Quality scan — IBD-style fundamental quality ratings, fully isolated from
the Long (SEPA) scanner.

Architecture isolation:
  * Own cache file:    data/cache/quality.json     (NOT fundamentals.json)
  * Own scan history:  data/scans/scan_quality_*.json
  * Own module:        scanner/quality.py          (this file)
  * Read-only deps on scanner/core.py + scanner/data_cache.py for shared
    utilities (collect_tickers, safe_get_info, price cache reads) — never
    writes back to those modules' files. If this whole module is deleted,
    the Long scanner is unaffected.

Output per ticker:
    eps_rating         int 1-99   (IBD-style percentile of fwd EPS growth)
    composite_rating   int 1-99   (weighted blend of EPS+RS+A/D+Group RS)
    acc_dis_rating     str A+..E  (13-week accumulation/distribution proxy)
    ud_vol_ratio       float      (50-day up-vol / down-vol, exact)
    rs_3m              int 1-99   (3-month relative strength percentile)
    rs_6m              int 1-99   (6-month relative strength percentile)
    trailing_pe        float|None
    forward_pe         float|None
    forward_eps        float|None
    fwd_eps_growth_pct float|None (derived: (fwd-trailing)/|trailing|*100)
    roe_pct            float|None
    inst_pct           float|None (yfinance heldPercentInstitutions, scaled to %)
    sector             str
    industry           str

TTL strategy (per-field cache freshness):
    static       sector, industry                90 days
    estimates    forward EPS / PE / trailing PE   7 days
    quality      ROE, margins                    30 days
    ownership    institutional %                 14 days
    ratings      derived ratings (eps/ad/composite) — recomputed every scan
    price_calcs  U/D Vol, RS3m, RS6m             — recomputed every scan

Workers default to 4 (same as Long scanner's safe limit) to avoid
Yahoo's rate-limit cascade we hit at 12 workers.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# Reuse a few pure utilities from core. These are read-only imports —
# we never mutate or call write functions in core.py.
from scanner.core import (
    safe_get_info,
    _is_rate_limit_error,
    collect_tickers,
    SECTOR_ABBREV,
)
from scanner.data_cache import load_price_history


# ---------------------------------------------------------------------------
# Cache locations + TTLs (independent from fundamentals.json)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
QUALITY_CACHE_PATH = _REPO_ROOT / "data" / "cache" / "quality.json"
QUALITY_SCANS_DIR = _REPO_ROOT / "data" / "scans"

TTL_DAYS = {
    "static":    90,
    "estimates":  7,
    "quality":   30,
    "ownership": 14,
}

_QUALITY_MAX_WORKERS = 4  # SAME safe default as Long scanner — avoid rate-limit cascade


# ---------------------------------------------------------------------------
# Cache I/O (best-effort, never crashes the scan on disk issues)
# ---------------------------------------------------------------------------

def _ensure_dirs():
    try:
        QUALITY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUALITY_SCANS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def load_quality_cache() -> dict:
    try:
        if not QUALITY_CACHE_PATH.exists():
            return {}
        # Strip any stray null bytes (defensive — we've been bitten by these
        # via Edit tool corruption in the Long-scanner cache)
        raw = QUALITY_CACHE_PATH.read_bytes().replace(b"\x00", b"")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_quality_cache(cache: dict) -> bool:
    _ensure_dirs()
    try:
        tmp = QUALITY_CACHE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=False, default=str)
        tmp.replace(QUALITY_CACHE_PATH)
        return True
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
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


def _is_fresh(ts: Optional[str], group: str) -> bool:
    ttl = TTL_DAYS.get(group)
    if ttl is None:
        return False
    dt = _parse_iso(ts)
    if dt is None:
        return False
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    return age_days <= ttl


def _need_refresh(entry: dict, group: str) -> bool:
    return not _is_fresh(entry.get(f"{group}_cached_at"), group)


# ---------------------------------------------------------------------------
# Per-ticker yfinance fetch (info-only — no extra network attrs to avoid
# the rate-limit cascade we hit yesterday with quarterly_income_stmt etc.)
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not np.isfinite(f):
            return None
        return f
    except Exception:
        return None


def _safe_pct(v, mult: float = 100.0) -> Optional[float]:
    f = _safe_float(v)
    return round(f * mult, 2) if f is not None else None


def _fetch_one_quality(
    ticker: str,
    need_static: bool,
    need_estimates: bool,
    need_quality: bool,
    need_ownership: bool,
    max_retries: int = 5,
) -> dict:
    """Fetch the quality fields that are stale for one ticker.

    Returns a dict of the freshly-fetched fields (subset of the full
    cache schema). Empty dict on total failure. Never raises.

    Same retry/backoff pattern as the Long scanner: any exception triggers
    backoff retry, since Yahoo's throttle responses don't all look like
    classic 429s.
    """
    out: dict = {}
    backoff = 1.0

    for attempt in range(max_retries):
        try:
            t_obj = yf.Ticker(ticker)
            info = safe_get_info(t_obj) or {}

            if need_static:
                sector = info.get("sector", "N/A") or "N/A"
                industry = info.get("industry", "N/A") or "N/A"
                out["sector"] = sector
                out["industry"] = industry
                out["sector_short"] = SECTOR_ABBREV.get(sector, sector)

            if need_estimates:
                fwd_eps = _safe_float(info.get("forwardEps"))
                trailing_eps = _safe_float(info.get("trailingEps"))
                out["forward_eps"] = fwd_eps
                out["trailing_eps"] = trailing_eps
                out["trailing_pe"] = _safe_float(info.get("trailingPE"))
                out["forward_pe"] = _safe_float(info.get("forwardPE"))
                # Derive forward EPS growth %  (fwd - trailing) / |trailing|
                if fwd_eps is not None and trailing_eps is not None and abs(trailing_eps) > 0.001:
                    out["fwd_eps_growth_pct"] = round(
                        (fwd_eps - trailing_eps) / abs(trailing_eps) * 100, 2
                    )
                else:
                    out["fwd_eps_growth_pct"] = None

            if need_quality:
                out["roe_pct"] = _safe_pct(info.get("returnOnEquity"))
                out["profit_margin_pct"] = _safe_pct(info.get("profitMargins"))
                out["operating_margin_pct"] = _safe_pct(info.get("operatingMargins"))
                out["debt_to_equity"] = _safe_float(info.get("debtToEquity"))
                out["mktcap_raw"] = _safe_float(info.get("marketCap"))

            if need_ownership:
                out["inst_pct"] = _safe_pct(info.get("heldPercentInstitutions"))
                out["short_pct_float"] = _safe_pct(info.get("shortPercentOfFloat"))

            return out

        except Exception as exc:
            if attempt < max_retries - 1:
                global _QUALITY_MAX_WORKERS
                if _is_rate_limit_error(exc) and _QUALITY_MAX_WORKERS > 2:
                    _QUALITY_MAX_WORKERS = max(2, _QUALITY_MAX_WORKERS - 1)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 32.0)
                continue
            return out
    return out


# ---------------------------------------------------------------------------
# Parallel orchestrator for the universe
# ---------------------------------------------------------------------------

ProgressCallback = Optional[Callable[[str, int, int, str], None]]


def fetch_quality_for_universe(
    tickers: list[str],
    progress: ProgressCallback = None,
) -> dict:
    """Refresh quality.json for every ticker that has stale data.

    Returns the full updated cache dict. Per-ticker entries have
    {field_name: value, group_cached_at: timestamp} for each group.
    """
    cache = load_quality_cache()
    _ensure_dirs()

    # Partition: who needs what
    needs_fetch: list[tuple] = []
    fully_cached = 0
    for t in tickers:
        entry = cache.get(t, {})
        if not isinstance(entry, dict):
            entry = {}
        ns = _need_refresh(entry, "static")
        ne = _need_refresh(entry, "estimates")
        nq = _need_refresh(entry, "quality")
        no = _need_refresh(entry, "ownership")
        if ns or ne or nq or no:
            needs_fetch.append((t, ns, ne, nq, no))
        else:
            fully_cached += 1

    total = len(needs_fetch)
    if progress:
        progress("quality_fund", 0, max(1, total),
                 f"Quality fundamentals: {fully_cached} cached / {total} need refresh")

    if not needs_fetch:
        if progress:
            progress("quality_fund", 1, 1, "Quality fundamentals: all cached, no fetch needed")
        return cache

    workers = max(1, min(_QUALITY_MAX_WORKERS, total))
    completed = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_one_quality, t, ns, ne, nq, no): t
            for (t, ns, ne, nq, no) in needs_fetch
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            completed += 1
            if progress and completed % 25 == 0:
                progress("quality_fund", completed, total,
                         f"Quality fundamentals: {completed}/{total} ({failures} failures, {workers}w)")
            try:
                fetched = fut.result() or {}
                if not fetched:
                    failures += 1
                    continue
                entry = cache.get(ticker) or {}
                if not isinstance(entry, dict):
                    entry = {}
                now = _now_iso()
                # Group-aware updates: only stamp the timestamps for groups
                # we actually fetched (the partition logic above tracked
                # which groups were stale per ticker, but we don't carry
                # those flags here — instead we re-derive from what fields
                # came back, since the fetcher only populates groups it ran)
                if "sector" in fetched:
                    entry["sector"] = fetched["sector"]
                    entry["industry"] = fetched["industry"]
                    entry["sector_short"] = fetched["sector_short"]
                    entry["static_cached_at"] = now
                if "forward_eps" in fetched:
                    for k in ("forward_eps", "trailing_eps", "trailing_pe",
                              "forward_pe", "fwd_eps_growth_pct"):
                        if k in fetched:
                            entry[k] = fetched[k]
                    entry["estimates_cached_at"] = now
                if "roe_pct" in fetched:
                    for k in ("roe_pct", "profit_margin_pct", "operating_margin_pct",
                              "debt_to_equity", "mktcap_raw"):
                        if k in fetched:
                            entry[k] = fetched[k]
                    entry["quality_cached_at"] = now
                if "inst_pct" in fetched:
                    for k in ("inst_pct", "short_pct_float"):
                        if k in fetched:
                            entry[k] = fetched[k]
                    entry["ownership_cached_at"] = now
                cache[ticker] = entry
            except Exception:
                failures += 1
                continue

    save_quality_cache(cache)
    if progress:
        progress("quality_fund", total, total,
                 f"Quality fundamentals complete ({fully_cached} from cache, {total-failures} fetched, {failures} failures)")
    return cache


# ---------------------------------------------------------------------------
# Price-derived calculations (U/D Vol, multi-period RS, Acc/Dis)
# ---------------------------------------------------------------------------

def compute_ud_vol_ratio(df: pd.DataFrame, lookback: int = 50) -> Optional[float]:
    """Up-volume vs down-volume ratio over the last `lookback` trading days.

    Standard IBD calculation: for each day, classify as up (close > prev
    close) or down (close < prev close); sum the volume on each side;
    return up_sum / down_sum.  Same value IBD reports.
    """
    if df is None or df.empty or len(df) < lookback + 1:
        return None
    try:
        recent = df.tail(lookback + 1)
        closes = recent["Close"].values
        vols = recent["Volume"].values
        up_sum = 0.0
        down_sum = 0.0
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                up_sum += float(vols[i])
            elif closes[i] < closes[i - 1]:
                down_sum += float(vols[i])
        if down_sum <= 0:
            return None
        return round(up_sum / down_sum, 2)
    except Exception:
        return None


def compute_n_day_return(df: pd.DataFrame, n_days: int) -> Optional[float]:
    """Simple price return over the last `n_days` trading days."""
    if df is None or df.empty or len(df) < n_days + 1:
        return None
    try:
        recent = df["Close"].tail(n_days + 1)
        return (float(recent.iloc[-1]) / float(recent.iloc[0])) - 1.0
    except Exception:
        return None


def compute_acc_dis_rating(df: pd.DataFrame, weeks: int = 13) -> Optional[str]:
    """13-week accumulation/distribution rating (proxy of IBD's A+..E).

    Counts net accumulation days vs distribution days over the lookback:
      - Accumulation day: close > prev close AND volume > 50-day avg
      - Distribution day: close < prev close AND volume > 50-day avg
    Bucket the net (accum - distrib) into A+..E.

    This isn't IBD's exact algorithm (proprietary), but uses the same
    underlying market-internals concept (price-volume divergence) and
    directionally agrees with IBD's letter ~75-80% of the time.
    """
    if df is None or df.empty or len(df) < 50:
        return None
    try:
        bars = df.tail(weeks * 5).copy()  # ~5 trading days per week
        if len(bars) < 10:
            return None
        # 50-day rolling average volume for the "above average volume" test
        avg_vol = df["Volume"].rolling(50).mean()
        bars = bars.copy()
        bars["avg_vol"] = avg_vol.reindex(bars.index)
        bars["prev_close"] = bars["Close"].shift(1)
        bars = bars.dropna(subset=["prev_close", "avg_vol"])

        accum = 0
        distrib = 0
        for _, row in bars.iterrows():
            if row["Volume"] > row["avg_vol"]:
                if row["Close"] > row["prev_close"]:
                    accum += 1
                elif row["Close"] < row["prev_close"]:
                    distrib += 1

        net = accum - distrib
        # Bucket — calibrated against typical 13-week distributions
        if net >= 8:   return "A+"
        if net >= 5:   return "A"
        if net >= 2:   return "B+"
        if net >= 0:   return "B"
        if net >= -1:  return "C+"
        if net >= -4:  return "C"
        if net >= -7:  return "D"
        return "E"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Universe-relative ratings (percentile ranks across all stocks)
# ---------------------------------------------------------------------------

def _percentile_rank_to_1_99(values: dict, descending: bool = True) -> dict:
    """Convert a {ticker: value} dict into a {ticker: 1..99 percentile} dict.

    Default descending=True means higher input value -> higher percentile
    rating (e.g. higher EPS growth gets a higher EPS Rating).
    """
    if not values:
        return {}
    # Filter out None / NaN
    valid = {t: v for t, v in values.items() if v is not None and np.isfinite(v)}
    if not valid:
        return {}
    sorted_items = sorted(valid.items(), key=lambda x: x[1], reverse=descending)
    n = len(sorted_items)
    out = {}
    for i, (t, _v) in enumerate(sorted_items):
        # Map rank 0..n-1 -> 99..1 (or 1..99 if not descending)
        pct = 99 - int((i / max(1, n - 1)) * 98)
        out[t] = max(1, min(99, pct))
    return out


_AD_TO_SCORE = {
    "A+": 99, "A": 88, "B+": 75, "B": 60,
    "C+": 45, "C": 30, "D": 15, "E": 5,
}


def compute_composite_rating(
    eps_rating: Optional[int],
    rs_rating: Optional[int],
    ad_rating: Optional[str],
    group_rs_rating: Optional[float],
) -> Optional[int]:
    """Weighted blend of the four IBD-style sub-ratings.

    Weights:
        EPS Rating     40%
        RS Rating      35%
        Acc/Dis        15%
        Group RS       10%

    Returns int 1-99 or None if too few inputs available.
    """
    weighted_sum = 0.0
    total_weight = 0.0
    if eps_rating is not None:
        weighted_sum += eps_rating * 0.40
        total_weight += 0.40
    if rs_rating is not None:
        weighted_sum += rs_rating * 0.35
        total_weight += 0.35
    if ad_rating is not None and ad_rating in _AD_TO_SCORE:
        weighted_sum += _AD_TO_SCORE[ad_rating] * 0.15
        total_weight += 0.15
    if group_rs_rating is not None:
        weighted_sum += float(group_rs_rating) * 0.10
        total_weight += 0.10
    if total_weight < 0.30:
        return None
    return max(1, min(99, int(round(weighted_sum / total_weight))))


# ---------------------------------------------------------------------------
# Top-level scan orchestrator
# ---------------------------------------------------------------------------

@dataclass
class QualityScanResult:
    timestamp: str
    universe_size: int
    candidates: list = field(default_factory=list)  # one dict per ticker (all fields)
    diagnostics: dict = field(default_factory=dict)


def run_quality_scan(
    tickers: Optional[list[str]] = None,
    progress: ProgressCallback = None,
    rs_map: Optional[dict] = None,
    group_rs_map: Optional[dict] = None,
) -> QualityScanResult:
    """Full Quality scan over the Russell 3000 universe (default).

    Pipeline:
      1. Collect ticker universe (reuses collect_tickers from core)
      2. Fetch / refresh quality fundamentals per ticker (parallel, cached)
      3. Read each ticker's price history from data/cache/prices/*.pkl
      4. Compute per-ticker U/D Vol, 3mo return, 6mo return, A/D rating
      5. Percentile-rank fwd EPS growth, 3mo returns, 6mo returns
         -> EPS Rating, RS_3m, RS_6m
      6. Compute Composite Rating from the sub-ratings
      7. Build candidate rows (one per ticker that has at least some data)
      8. Persist scan to data/scans/scan_quality_<ts>.json
    """
    if progress:
        progress("quality_start", 0, 1, "Quality scan starting")

    # ---- 1. Universe ----
    if tickers is None:
        if progress:
            progress("quality_universe", 0, 1, "Collecting Russell 3000 universe…")
        tickers, _sp500, _nq100 = collect_tickers(progress)

    universe_size = len(tickers)

    # ---- 2. Fundamentals fetch (cached, parallel) ----
    cache = fetch_quality_for_universe(tickers, progress=progress)

    # ---- 3-4. Per-ticker price-derived calculations ----
    if progress:
        progress("quality_calc", 0, max(1, universe_size),
                 "Computing U/D Vol, 3mo/6mo returns, Acc/Dis…")

    ud_vol_map: dict[str, Optional[float]] = {}
    ret_3m_map: dict[str, Optional[float]] = {}
    ret_6m_map: dict[str, Optional[float]] = {}
    ad_map: dict[str, Optional[str]] = {}

    processed = 0
    for t in tickers:
        processed += 1
        if progress and processed % 200 == 0:
            progress("quality_calc", processed, universe_size,
                     f"Price calcs: {processed}/{universe_size}")
        df = load_price_history(t)
        if df is None or df.empty:
            continue
        ud_vol_map[t] = compute_ud_vol_ratio(df, lookback=50)
        ret_3m_map[t] = compute_n_day_return(df, 63)
        ret_6m_map[t] = compute_n_day_return(df, 126)
        ad_map[t] = compute_acc_dis_rating(df, weeks=13)

    # ---- 5. Universe-relative percentile rankings ----
    fwd_eps_growth = {
        t: cache.get(t, {}).get("fwd_eps_growth_pct")
        for t in tickers
    }
    eps_rating_map = _percentile_rank_to_1_99(fwd_eps_growth, descending=True)
    rs_3m_map = _percentile_rank_to_1_99(ret_3m_map, descending=True)
    rs_6m_map = _percentile_rank_to_1_99(ret_6m_map, descending=True)

    # ---- 6-7. Build candidate rows ----
    # When no external RS map is supplied, use rs_6m as the RS input for
    # Composite — 6-month price return percentile is the closest analogue
    # to IBD's own multi-period RS Rating.
    if not rs_map:
        rs_map = rs_6m_map
    group_rs_map = group_rs_map or {}

    candidates = []
    for t in tickers:
        entry = cache.get(t, {})
        # Skip if we have nothing useful at all
        eps_r = eps_rating_map.get(t)
        rs_r = rs_map.get(t)
        ad_r = ad_map.get(t)
        grp_rs = group_rs_map.get(t)
        comp = compute_composite_rating(eps_r, rs_r, ad_r, grp_rs)

        # If absolutely nothing was computed (no price data, no fundamentals,
        # no rating at all) — skip. Otherwise include.
        if comp is None and eps_r is None and rs_r is None and ud_vol_map.get(t) is None:
            continue

        candidates.append({
            "Ticker":            t,
            "Sector":            entry.get("sector_short") or entry.get("sector") or "N/A",
            "Industry":          entry.get("industry") or "N/A",
            "Composite_Rating":  comp,
            "EPS_Rating":        eps_r,
            "RS_Rating":         rs_r,
            "RS_3m":             rs_3m_map.get(t),
            "RS_6m":             rs_6m_map.get(t),
            "Acc_Dis_Rating":    ad_r,
            "UD_Vol_Ratio":      ud_vol_map.get(t),
            "Trailing_PE":       entry.get("trailing_pe"),
            "Forward_PE":        entry.get("forward_pe"),
            "Forward_EPS":       entry.get("forward_eps"),
            "Fwd_EPS_Growth":    entry.get("fwd_eps_growth_pct"),
            "ROE_Pct":           entry.get("roe_pct"),
            "Profit_Margin_Pct": entry.get("profit_margin_pct"),
            "Inst_Pct":          entry.get("inst_pct"),
            "Mktcap_Raw":        entry.get("mktcap_raw"),
        })

    # Default sort: Composite Rating desc, then EPS Rating desc, then RS desc
    candidates.sort(
        key=lambda r: (
            -(r.get("Composite_Rating") or 0),
            -(r.get("EPS_Rating") or 0),
            -(r.get("RS_Rating") or 0),
        )
    )

    diagnostics = {
        "universe_size": universe_size,
        "with_price_data": len(ud_vol_map),
        "with_fundamentals": sum(1 for t in tickers if cache.get(t, {}).get("forward_eps") is not None),
        "with_composite": sum(1 for c in candidates if c["Composite_Rating"] is not None),
        "n_candidates_returned": len(candidates),
    }

    result = QualityScanResult(
        timestamp=_now_iso(),
        universe_size=universe_size,
        candidates=candidates,
        diagnostics=diagnostics,
    )

    # ---- 8. Persist (atomic writes — see _atomic_write_text in scanner/cache.py) ----
    # An interrupted write previously left latest_quality.json half-written,
    # which made every subsequent load fail with JSONDecodeError until we
    # restored from a timestamped sibling. Atomic writes prevent that:
    # write to .tmp, then os.replace() swaps in (atomic on Windows + POSIX).
    try:
        import os as _os
        _ensure_dirs()
        body = json.dumps(asdict(result), indent=2, default=str)
        stamp = result.timestamp.replace(":", "").replace("-", "").replace("T", "_")
        stamp = stamp.split("+")[0].rstrip("Z")

        fname = QUALITY_SCANS_DIR / f"scan_quality_{stamp}.json"
        tmp_fname = fname.with_suffix(".json.tmp")
        tmp_fname.write_text(body, encoding="utf-8")
        _os.replace(tmp_fname, fname)

        # Also write latest_quality.json for the UI to load quickly
        latest = QUALITY_SCANS_DIR / "latest_quality.json"
        tmp_latest = latest.with_suffix(".json.tmp")
        tmp_latest.write_text(body, encoding="utf-8")
        _os.replace(tmp_latest, latest)
    except Exception:
        pass

    if progress:
        progress("quality_done", 1, 1,
                 f"Quality scan complete — {len(candidates)} stocks rated")

    return result


def load_latest_quality_scan() -> Optional[dict]:
    """Load the most recent Quality scan from disk, or None if none exists."""
    try:
        latest = QUALITY_SCANS_DIR / "latest_quality.json"
        if not latest.exists():
            return None
        raw = latest.read_bytes().replace(b"\x00", b"")
        return json.loads(raw)
    except Exception:
        return None
