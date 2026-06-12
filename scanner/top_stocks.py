"""
Top Stocks — the intersection of the SEPA Long scanner and the IBD Quality scanner.

Pure data layer. No UI, no I/O beyond loading the two latest scan JSONs.

Vision:
    Only the very best stocks should show up when the user opens the app.
    "Best" = passes BOTH the technical SEPA criteria (Long scan) AND
    the IBD-elite fundamental criteria (Quality scan).

How it works:
    1. Load latest.json (Long scan)        -> ~144 SEPA candidates
    2. Load latest_quality.json (Quality)  -> ~6,888 rated universe
    3. Intersect on Ticker symbol          -> typically equals the Long count
       (since Quality covers the whole universe)
    4. Merge fields from both scans into one row per ticker
    5. Apply user's filter sliders:
         Quality side (IBD-elite defaults):
           Composite Rating  >= 80
           EPS Rating        >= 80
           Acc/Dis Rating    >= B+
           U/D Vol Ratio     >= 1.5
         Long side:
           RSI               <= 80   (avoid starting overbought positions)
    6. Sort by Composite Rating descending

Freshness:
    Both scans must be <= 48 hours old. If either is stale or missing,
    the UI shows a warning + empty page until the user runs them.

If this module is deleted, the rest of the app continues to function;
the Top Stocks page would simply 404.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from .cache import load_latest_scan
from .quality import load_latest_quality_scan


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Acc/Dis rating ordering (worst -> best). Used to compare letter grades
# numerically for the slider filter. Mirrors the order in ui/quality_view.py
# so the two never disagree on what "B+ or better" means.
AD_ORDER = {
    "E":  0,
    "D-": 1, "D": 2, "D+": 3,
    "C-": 4, "C": 5, "C+": 6,
    "B-": 7, "B": 8, "B+": 9,
    "A-": 10, "A": 11, "A+": 12,
}

# IBD-elite default thresholds (loaded into the sliders on first paint).
DEFAULT_MIN_COMPOSITE   = 80
DEFAULT_MIN_EPS         = 80
DEFAULT_MIN_AD          = "B+"
DEFAULT_MIN_UD_VOL      = 1.5
DEFAULT_MAX_RSI         = 80      # avoid starting positions in overbought names

# Freshness — both scans must be at most this old for the intersection to be
# considered valid. The user said "up to 48 hours old is fine."
FRESHNESS_HOURS = 48


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TopStocksResult:
    """One pass of the intersection + filtering pipeline."""

    # Raw counts (for the summary tiles)
    long_count: int = 0          # SEPA candidates in latest Long scan
    quality_count: int = 0       # Rated stocks in latest Quality scan
    intersection_count: int = 0  # Stocks present in BOTH scans
    elite_count: int = 0         # Survivors after all slider filters

    # Freshness
    long_timestamp: Optional[str] = None
    quality_timestamp: Optional[str] = None
    long_fresh: bool = False
    quality_fresh: bool = False

    # The actual table rows (each is a merged dict)
    candidates: list[dict] = field(default_factory=list)

    @property
    def both_fresh(self) -> bool:
        return self.long_fresh and self.quality_fresh

    @property
    def either_missing(self) -> bool:
        return self.long_timestamp is None or self.quality_timestamp is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a stored UTC ISO timestamp into a tz-aware datetime."""
    if not ts:
        return None
    try:
        s = ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_fresh(ts: Optional[str], hours: int = FRESHNESS_HOURS) -> bool:
    """True if the timestamp is within the given window (default 48h)."""
    dt = _parse_iso(ts)
    if dt is None:
        return False
    try:
        delta = datetime.now(timezone.utc) - dt
        return delta <= timedelta(hours=hours)
    except Exception:
        return False


def _safe_num(v, default=None):
    """Coerce v to a number if it already is one; otherwise return default."""
    if isinstance(v, bool):
        # bools are technically ints in Python; treat them as "not numeric"
        return default
    if isinstance(v, (int, float)):
        return v
    return default


def _ad_rank(letter) -> int:
    """A/D letter grade -> 0-12 ordinal. Unknown grades map to 0 (worst)."""
    if not isinstance(letter, str):
        return 0
    return AD_ORDER.get(letter, 0)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_top_stocks(
    min_composite: int = DEFAULT_MIN_COMPOSITE,
    min_eps: int = DEFAULT_MIN_EPS,
    min_ad: str = DEFAULT_MIN_AD,
    min_ud_vol: float = DEFAULT_MIN_UD_VOL,
    max_rsi: float = DEFAULT_MAX_RSI,
) -> TopStocksResult:
    """Compute the intersection of the latest Long + Quality scans, then filter.

    All filter args have IBD-elite defaults. The UI passes the user's
    slider values; if no UI is wired up, the defaults produce a sensible
    "elite picks" list out of the box.
    """
    long_scan = load_latest_scan()
    quality_scan = load_latest_quality_scan()

    result = TopStocksResult()

    # ---- Freshness check ----
    if long_scan:
        result.long_timestamp = long_scan.get("timestamp")
        result.long_fresh = _is_fresh(result.long_timestamp)
        result.long_count = len(long_scan.get("candidates", []))
    if quality_scan:
        result.quality_timestamp = quality_scan.get("timestamp")
        result.quality_fresh = _is_fresh(result.quality_timestamp)
        result.quality_count = len(quality_scan.get("candidates", []))

    # If either scan is missing or stale, return empty. The UI will show
    # a warning banner + Run Scan shortcuts instead of partial data.
    if not result.both_fresh:
        return result

    # ---- Build lookup indexes ----
    long_by_ticker    = {c["Ticker"]: c for c in long_scan["candidates"] if "Ticker" in c}
    quality_by_ticker = {c["Ticker"]: c for c in quality_scan["candidates"] if "Ticker" in c}

    intersection = set(long_by_ticker) & set(quality_by_ticker)
    result.intersection_count = len(intersection)

    # ---- Merge + filter ----
    min_ad_rank = _ad_rank(min_ad)
    rows: list[dict] = []
    for ticker in intersection:
        l = long_by_ticker[ticker]
        q = quality_by_ticker[ticker]

        # Quality filters
        comp = _safe_num(q.get("Composite_Rating"))
        if comp is None or comp < min_composite:
            continue

        eps = _safe_num(q.get("EPS_Rating"))
        if eps is None or eps < min_eps:
            continue

        if _ad_rank(q.get("Acc_Dis_Rating")) < min_ad_rank:
            continue

        ud = _safe_num(q.get("UD_Vol_Ratio"))
        if ud is None or ud < min_ud_vol:
            continue

        # Long-side filter (RSI). If RSI is missing, don't filter it out —
        # the Long scan wouldn't have surfaced it without RSI being computed,
        # but be defensive in case of legacy rows.
        rsi = _safe_num(l.get("RSI"))
        if rsi is not None and rsi > max_rsi:
            continue

        # Build the merged row. Order chosen for the table columns:
        # identity | quality ratings | technical (RSI, RS) | trade plan | tags
        rows.append({
            "Ticker":              ticker,
            "Sector":              q.get("Sector") or l.get("Sector", ""),
            "Industry":            q.get("Industry") or l.get("Industry", ""),
            # Industry_Rank from the Momentum scan (1 = strongest industry).
            # O'Neil: ~49% of a stock's price move is attributable to its
            # industry group. Surfacing this lets the UI filter to top-N.
            "Industry_Rank":       l.get("Industry_Rank"),
            "Composite_Rating":    comp,
            "EPS_Rating":          eps,
            "Acc_Dis_Rating":      q.get("Acc_Dis_Rating", ""),
            "UD_Vol_Ratio":        ud,
            "RS_Rating":           q.get("RS_Rating"),
            "RSI":                 rsi,
            "Price":               l.get("Price"),
            "Buy_Trigger":         l.get("Buy_Trigger"),
            "Stop":                l.get("Stop"),
            "Risk_Pct":            l.get("Risk_Pct"),
            "Signals":             l.get("Signals", []) or [],
            "Warnings":            l.get("Warnings", []) or [],
            "Fwd_EPS_Growth":      q.get("Fwd_EPS_Growth"),
            # Carry the full Long + Quality dicts on the side in case the
            # deep dive or future features want richer data without a re-fetch.
            "_long":               l,
            "_quality":            q,
        })

    # ---- Sort: Composite Rating descending (user's chosen default) ----
    # Tie-breaker chain: Composite -> EPS -> RS_Rating -> Ticker (alpha)
    # so identical Composite scores get a stable, sensible secondary order.
    def _sort_key(r):
        return (
            -(r.get("Composite_Rating") or 0),
            -(r.get("EPS_Rating") or 0),
            -(r.get("RS_Rating") or 0),
            r.get("Ticker", ""),
        )
    rows.sort(key=_sort_key)

    result.candidates = rows
    result.elite_count = len(rows)
    return result
