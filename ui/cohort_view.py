"""
BUY ZONE Cohort Tracker — dialog UI that shows forward-return stats over every
historical BUY ZONE flag.

Reached via the "📊 COHORT TRACKER" button on the Momentum tab. Opens a modal
dialog that answers the question the scanner exists to answer:

    Is the scanner finding real winners, or is it putting me on the wrong scent?

Data flow
---------
1. `load_cohort()` (data layer) — reads every historical flag from
   data/buy_zone_cohort.jsonl.
2. For each flag, we load the ticker's price history from data_cache and slice
   from `flag_date` to today. That gives us: current close, max return, max
   drawdown, and a "worked?" flag (did the price hit +2R before -1R, using the
   flagged pivot and stop as the R anchor?).
3. Aggregate into cohort-level rollups: hit rate at +1R/+2R, avg return at
   5/10/20 sessions, sector composition.

All computation is done on-load — no persistent forward-return cache. Given
we're processing at most ~100 flags at once and price history is already cached
per ticker, this is fast enough (~1-2 seconds).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import streamlit as st

from scanner.buy_zone_cohort import load_cohort, cohort_summary
from scanner.cohort_history import append_snapshot, load_history, get_previous_snapshot
from scanner.data_cache import load_price_history
from ui.style import PALETTE


# Minimum trading sessions of forward history a flag must have before it
# counts toward the REACHED +5% / REACHED +10% hit-rate denominators.
# Rationale (user request 2026-07-22): younger flags haven't had a fair
# chance to move — including them dragged the hit rate down artificially.
# 5 trading sessions ≈ one week, which matches Minervini's "winners work
# fast" heuristic and is the smallest window where a genuine breakout has
# had time to distinguish itself from a dead-cat bounce. Flags younger
# than this are shown as a separate "developing" count.
MATURITY_TRADING_DAYS = 5


# ---------------------------------------------------------------------------
# Per-flag forward-return computation
# ---------------------------------------------------------------------------

def _compute_forward_metrics(row: dict) -> dict:
    """Given a cohort row (flag context), compute forward-return metrics.

    Returns dict with:
      current_close, current_return_pct, days_since,
      max_return_pct, max_drawdown_pct,
      hit_2r_before_neg_1r (True/False/None if unknowable),
      close_5d, close_10d, close_20d,
      return_5d_pct, return_10d_pct, return_20d_pct

    Returns dict of None values if price history isn't available or the flag
    date is missing.
    """
    empty = {
        "current_close":         None,
        "current_return_pct":    None,
        "days_since":            None,
        "trading_days_since":    None,
        "max_return_pct":        None,
        "max_drawdown_pct":      None,
        "hit_2r_before_neg_1r":  None,
        "date_reached_5pct":     None,
        "date_reached_10pct":    None,
        "days_to_5pct":          None,
        "days_to_10pct":         None,
        "close_5d":              None,
        "close_10d":             None,
        "close_20d":             None,
        "return_5d_pct":         None,
        "return_10d_pct":        None,
        "return_20d_pct":        None,
    }

    ticker = row.get("ticker")
    flag_date_str = row.get("flag_date")
    flag_close = row.get("flag_close")
    if not (ticker and flag_date_str and flag_close):
        return empty

    try:
        flag_close = float(flag_close)
    except (TypeError, ValueError):
        return empty

    df = load_price_history(ticker)
    if df is None or df.empty or "Close" not in df.columns:
        return empty

    # Normalize the DataFrame's index to date so we can slice by flag_date.
    try:
        idx = pd.to_datetime(df.index).date
    except Exception:
        return empty

    try:
        flag_dt = datetime.strptime(flag_date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return empty

    # Slice from the first bar on or after the flag date onward.
    mask = idx >= flag_dt
    if not mask.any():
        return empty
    post = df[mask]
    if post.empty:
        return empty

    closes = post["Close"].astype(float)
    current_close = float(closes.iloc[-1])
    max_close = float(closes.max())
    min_close = float(closes.min())

    # Session count between flag and now (rough days-since via index length)
    days_since = int((date.today() - flag_dt).days)
    # Trading sessions of forward history (bars after the flag bar, inclusive
    # of any bar dated on the flag_date itself). Used for maturity check —
    # flags with fewer than MATURITY_TRADING_DAYS bars haven't had a fair
    # chance to hit +5% / +10% and are excluded from the hit-rate denominator.
    trading_days_since = max(0, len(post) - 1)

    current_return_pct = round((current_close - flag_close) / flag_close * 100, 2)
    max_return_pct     = round((max_close - flag_close) / flag_close * 100, 2)
    max_drawdown_pct   = round((min_close - flag_close) / flag_close * 100, 2)

    # +2R-before-(-1R) check — CORRECT Minervini/trader semantics: the trade
    # doesn't start until price CLEARS THE PIVOT. Before the pivot is broken,
    # the stop is not in effect — there's no open trade. Only after pivot is
    # cleared do we watch for the +2R vs -1R race.
    #
    # This fixes a user-reported bug (2026-07-10, MRVL): flag was pivot $252.96,
    # stop $244, and price dropped to $243.27 without ever breaching pivot.
    # Old logic counted this as a loss ("stop was hit"). Correct logic: no
    # trade was ever entered, so the flag remains undecided (…).
    #
    # Four possible outcomes now:
    #   True  → pivot cleared AND hit +2R first ("worked")
    #   False → pivot cleared AND hit -1R first ("failed")
    #   None  → EITHER pivot never cleared OR still in-flight after entry
    # If we don't have both pivot and stop, skip entirely (None).
    hit_2r = None
    pivot_cleared = False
    pivot = row.get("flag_pivot")
    stop = row.get("flag_stop")
    try:
        pivot_f = float(pivot) if pivot is not None else None
        stop_f  = float(stop)  if stop  is not None else None
    except (TypeError, ValueError):
        pivot_f = stop_f = None
    if pivot_f and stop_f and pivot_f > stop_f:
        risk_per_share = pivot_f - stop_f
        r_plus_2  = pivot_f + 2 * risk_per_share
        r_minus_1 = stop_f  # -1R = stop hit (after entry)
        # Two-phase walk:
        #   Phase 1: Wait for pivot to clear. Stops don't apply yet.
        #   Phase 2: Once pivot clears, race for +2R vs -1R.
        for c in closes:
            c = float(c)
            if not pivot_cleared:
                if c >= pivot_f:
                    pivot_cleared = True
                # Before entry: no decision either way — just keep watching.
                continue
            # After entry: first level touched wins.
            if c <= r_minus_1:
                hit_2r = False
                break
            if c >= r_plus_2:
                hit_2r = True
                break
        # If we exit the loop without hit_2r being set, it stays None:
        #   • Pivot never cleared → never entered → undecided
        #   • Pivot cleared but neither +2R nor -1R hit yet → still in-flight

    def _close_at_session_offset(n: int) -> Optional[float]:
        if len(closes) > n:
            return float(closes.iloc[n])
        return None

    close_5d  = _close_at_session_offset(5)
    close_10d = _close_at_session_offset(10)
    close_20d = _close_at_session_offset(20)

    def _pct(target: Optional[float]) -> Optional[float]:
        if target is None:
            return None
        return round((target - flag_close) / flag_close * 100, 2)

    # ---- Milestone dates: FIRST session where a REALIZABLE trade would
    # have hit +5% (or +10%) above the flag close. Uses STRICT semantics
    # (user request 2026-07-25):
    #
    #   1. Requires pivot to have cleared FIRST — no pivot clear = no
    #      trade ever entered = milestone disqualified. Same rule as
    #      the +2R hit-rate tile above.
    #   2. Once pivot clears, if any subsequent close is at or below
    #      the stop level BEFORE reaching the milestone, the milestone
    #      is disqualified — the trader would have been stopped out
    #      first and never captured the gain.
    #
    # This makes REACHED +5% and REACHED +10% honest measures of
    # "would this have worked as a real trade?" rather than pure
    # cohort momentum. If pivot or stop aren't available (legacy flags),
    # falls back to lenient measurement (just checks the price target).
    target_5  = flag_close * 1.05
    target_10 = flag_close * 1.10
    date_reached_5pct  = None
    date_reached_10pct = None
    days_to_5pct       = None
    days_to_10pct      = None
    try:
        # Local pivot/stop parsing — separate from the pivot_cleared /
        # stop_f already computed above, since those are only set when
        # BOTH exist AND pivot > stop. For milestone tracking, do the
        # same rigor.
        _pivot_f = pivot_f if (pivot_f and stop_f and pivot_f > stop_f) else None
        _stop_f  = stop_f  if (pivot_f and stop_f and pivot_f > stop_f) else None

        milestone_pivot_cleared = False
        stopped_out = False
        for i, (idx_val, c_val) in enumerate(zip(post.index, closes)):
            c_val = float(c_val)

            # Phase 1: waiting for pivot to clear (only applies when we
            # have pivot data — otherwise skip straight to milestone check
            # so legacy flags without pivot/stop still get lenient tracking).
            if _pivot_f is not None and not milestone_pivot_cleared:
                if c_val >= _pivot_f:
                    milestone_pivot_cleared = True
                # Before entry, no milestone can be credited even if price
                # briefly spikes above target — we never actually held the trade.
                continue

            # Phase 2: entered the trade (or lenient mode with no pivot).
            # First check stop-out — if we're stopped, milestones from here
            # on are disqualified.
            if _stop_f is not None and c_val <= _stop_f:
                stopped_out = True
                break

            # Not stopped out — check milestones.
            if date_reached_5pct is None and c_val >= target_5:
                try:
                    date_reached_5pct = pd.to_datetime(idx_val).strftime("%Y-%m-%d")
                except Exception:
                    date_reached_5pct = str(idx_val)[:10]
                days_to_5pct = i  # 0 = flag bar itself, 1 = next session, etc.
            if date_reached_10pct is None and c_val >= target_10:
                try:
                    date_reached_10pct = pd.to_datetime(idx_val).strftime("%Y-%m-%d")
                except Exception:
                    date_reached_10pct = str(idx_val)[:10]
                days_to_10pct = i
            if date_reached_5pct is not None and date_reached_10pct is not None:
                break

        # If we exited without pivot ever clearing, both milestones stay
        # None (undecided-never-entered) — that's the correct strict answer.
        # If we exited via stopped_out, milestones already recorded before
        # the stop hit stand; any that weren't reached before the stop stay None.
    except Exception:
        pass

    return {
        "current_close":         round(current_close, 2),
        "current_return_pct":    current_return_pct,
        "days_since":            days_since,
        "trading_days_since":    trading_days_since,
        "max_return_pct":        max_return_pct,
        "max_drawdown_pct":      max_drawdown_pct,
        "hit_2r_before_neg_1r":  hit_2r,
        "date_reached_5pct":     date_reached_5pct,
        "date_reached_10pct":    date_reached_10pct,
        "days_to_5pct":          days_to_5pct,
        "days_to_10pct":         days_to_10pct,
        "close_5d":              round(close_5d, 2) if close_5d else None,
        "close_10d":             round(close_10d, 2) if close_10d else None,
        "close_20d":             round(close_20d, 2) if close_20d else None,
        "return_5d_pct":         _pct(close_5d),
        "return_10d_pct":        _pct(close_10d),
        "return_20d_pct":        _pct(close_20d),
    }


def _build_cohort_df() -> pd.DataFrame:
    """Load cohort log + enrich with forward-return metrics. Newest first.

    Dedupes by ticker — a ticker that flagged multiple times (Mon/Tue/Wed)
    counts as ONE cohort member anchored to the EARLIEST flag. This treats
    each ticker as a single "setup we spotted" tracked from first sighting,
    rather than counting the same setup 3x. The Times Flagged column shows
    the raw re-flag count for transparency.

    User request 2026-07-22 — was previously one row per flag event, which
    let a single active setup dominate the cohort stats.
    """
    entries = load_cohort()
    if not entries:
        return pd.DataFrame()

    # Dedupe: for each ticker, keep the earliest flag_date entry AND count
    # how many total flag events there were. Iterating over the full log
    # once (rather than groupby-in-pandas) so we can also count re-flags.
    earliest_by_ticker: dict[str, dict] = {}
    counts_by_ticker: dict[str, int] = {}
    for e in entries:
        t = str(e.get("ticker") or "").upper()
        if not t:
            continue
        counts_by_ticker[t] = counts_by_ticker.get(t, 0) + 1
        fd = str(e.get("flag_date") or "")
        cur = earliest_by_ticker.get(t)
        if cur is None or fd < str(cur.get("flag_date") or ""):
            earliest_by_ticker[t] = e

    rows = []
    for t, e in earliest_by_ticker.items():
        fwd = _compute_forward_metrics(e)
        rows.append({
            "Flag Date":      e.get("flag_date"),
            "Ticker":         t,
            "Times Flagged":  counts_by_ticker.get(t, 1),
            "Sector":         e.get("flag_sector"),
            "Signal":         e.get("flag_market_signal"),
            "Flag $":         e.get("flag_close"),
            "Now $":          fwd.get("current_close"),
            "Days":           fwd.get("days_since"),
            "TDays":          fwd.get("trading_days_since"),
            "Return %":       fwd.get("current_return_pct"),
            "Max %":          fwd.get("max_return_pct"),
            "MaxDD %":        fwd.get("max_drawdown_pct"),
            # Milestone dates — populated by _compute_forward_metrics.
            # None if the ticker never reached that milestone. Used by
            # the "Reached +5% / +10%" expander tables below the tiles.
            "Reached 5%":     fwd.get("date_reached_5pct"),
            "Reached 10%":    fwd.get("date_reached_10pct"),
            "Days→5%":        fwd.get("days_to_5pct"),
            "Days→10%":       fwd.get("days_to_10pct"),
            "5d %":           fwd.get("return_5d_pct"),
            "10d %":          fwd.get("return_10d_pct"),
            "20d %":          fwd.get("return_20d_pct"),
            "Worked?":        (
                "✓" if fwd.get("hit_2r_before_neg_1r") is True else
                "✗" if fwd.get("hit_2r_before_neg_1r") is False else
                "…"
            ),
            "VCP":            e.get("flag_vcp_quality"),
            "Pressure":       e.get("flag_pressure_20d"),
            "% > 50MA":       e.get("flag_pct_above_50ma"),
        })
    df = pd.DataFrame(rows)
    # Newest first by flag date (matches the old behavior for the table).
    if not df.empty:
        df = df.sort_values("Flag Date", ascending=False).reset_index(drop=True)
    return df


def _rollup_stats(df: pd.DataFrame) -> dict:
    """Cohort-level rollup: hit rates + avg returns.

    Split into MATURE and DEVELOPING buckets based on trading days since first
    flag. Hit rates (REACHED +5% / +10%) compute ONLY on the mature bucket so
    fresh flags don't drag down the ratios.
    """
    if df.empty:
        return {}

    # Split into mature / developing. TDays column comes from
    # _compute_forward_metrics via _build_cohort_df — it's the number of
    # trading bars we have post-flag. Flags without a TDays value (missing
    # price history) are treated as immature and excluded from hit-rate calc.
    tdays = pd.to_numeric(df.get("TDays"), errors="coerce")
    mature_mask     = tdays >= MATURITY_TRADING_DAYS
    developing_mask = ~mature_mask
    mature_df       = df[mature_mask]
    developing_df   = df[developing_mask]

    # Only count decided outcomes for hit rate (ignore "…" still-in-flight).
    # 2R hit rate keeps using the full df — the pivot-clear semantics are
    # already "wait until the trade actually starts", so young flags are
    # correctly counted as "…" (still-in-flight) and excluded automatically.
    decided = df[df["Worked?"].isin(["✓", "✗"])]
    n_dec  = len(decided)
    n_wins = int((decided["Worked?"] == "✓").sum())

    hit_rate_2r = (n_wins / n_dec * 100) if n_dec else None

    # Average N-day returns — only over flags that have that much history.
    def _avg(col: str) -> Optional[float]:
        s = df[col].dropna()
        if s.empty:
            return None
        return round(float(s.mean()), 2)

    # Hit rate at +5% / +10% — computed only on the MATURE bucket. Flags
    # with fewer than MATURITY_TRADING_DAYS bars of forward history are
    # excluded from both numerator and denominator, so freshly-flagged
    # tickers don't drag the ratio down while they haven't had a chance
    # to move yet.
    #
    # STRICT semantics (bug fix 2026-08-04): counts REACHABLE milestones only.
    # A flag counts as "reached +N%" only if its `Reached N%` column is
    # non-null, which comes from _compute_forward_metrics and requires:
    #   (1) pivot cleared before the milestone was hit
    #   (2) stop not hit before the milestone was hit
    # Legacy: this previously used Max % (any peek above +N% at any moment,
    # regardless of pivot / stop). That inflated the tiles vs. the drill-down
    # tables which already used the strict rule, causing user-visible
    # mismatches (e.g. tile said 9 tickers reached +10%, table showed 5).
    # Tiles and tables now agree.
    def _reached_rate(milestone_col: str) -> Optional[float]:
        if mature_df.empty or milestone_col not in mature_df.columns:
            return None
        n_mature = len(mature_df)
        n_reached = int(mature_df[milestone_col].notna().sum())
        return round(n_reached / n_mature * 100, 1) if n_mature else None

    return {
        "n_total":       len(df),
        "n_mature":      int(mature_mask.sum()),
        "n_developing":  int(developing_mask.sum()),
        "n_decided":     n_dec,
        "hit_rate_2r":   round(hit_rate_2r, 1) if hit_rate_2r is not None else None,
        # Key names retained (hit_max_5pct / hit_max_10pct) so the historical
        # cohort_history.jsonl schema stays compatible for chart continuity.
        # The semantic content is now strict, not lenient — snapshots written
        # from this point forward will be lower than pre-fix snapshots, so
        # expect a one-time step-down on the trend chart on the day this ships.
        "hit_max_5pct":  _reached_rate("Reached 5%"),
        "hit_max_10pct": _reached_rate("Reached 10%"),
        "avg_5d":        _avg("5d %"),
        "avg_10d":       _avg("10d %"),
        "avg_20d":       _avg("20d %"),
        "avg_current":   _avg("Return %"),
    }


def _sector_composition(df: pd.DataFrame) -> pd.DataFrame:
    """Count of flags per sector — used as a regime-tell.

    Expands yfinance's abbreviated sector labels (Comm.Svc, Cons.Def, etc.)
    to their full GICS-standard names so the display is unambiguous. If a
    sector isn't in the map, its raw label passes through unchanged.
    """
    if df.empty or "Sector" not in df.columns:
        return pd.DataFrame()
    counts = df["Sector"].fillna("N/A").value_counts().reset_index()
    counts.columns = ["Sector", "Flags"]
    # Map yfinance shortnames → full GICS-style labels
    _SECTOR_FULL_NAME = {
        "Tech":       "Technology",
        "Health":     "Healthcare",
        "Financial":  "Financial Services",
        "Comm.Svc":   "Communication Services",
        "Cons.Def":   "Consumer Defensive",
        "Cons.Cyc":   "Consumer Cyclical",
        "Indust.":    "Industrials",
        "Utils":      "Utilities",
        "RE":         "Real Estate",
        # Some scans emit the full name already; leave as-is
    }
    counts["Sector"] = counts["Sector"].map(lambda s: _SECTOR_FULL_NAME.get(s, s))
    return counts


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

@st.dialog("BUY ZONE Cohort Tracker", width="large")
def cohort_tracker_dialog():
    """Modal dialog showing every historical BUY ZONE flag + forward-return stats."""
    muted = PALETTE["text_muted"]
    cyan  = PALETTE["accent_cyan"]
    up    = PALETTE["up_green"]
    down  = PALETTE["down_red"]

    st.markdown("### Why this exists")
    st.markdown(
        "The scanner's job is to compress ~3,000 tickers into a short list of "
        "high-quality candidates. But how do we know if that list actually "
        "contains the winners? Without measuring the forward performance of "
        "**every** BUY ZONE flag — not just the ones you take a trade on — "
        "you can't answer *\"is the scanner finding real leaders, or putting "
        "me on the wrong scent?\"*"
    )
    st.markdown(
        "This tracker logs every BUY ZONE ticker on the day it's flagged, "
        "then reads its actual forward price action from cache to compute "
        "cohort-level performance. After 4-8 weeks of data, the numbers here "
        "tell you honestly whether the scanner is doing its job — turning a "
        "vague feeling into a measurable answer."
    )

    st.markdown("---")

    # ---- Load + compute ----
    with st.spinner("Loading cohort + computing forward returns…"):
        df = _build_cohort_df()

    if df.empty:
        st.info(
            "No BUY ZONE flags logged yet. Run a fresh Momentum scan and any "
            "BUY ZONE candidates will be captured automatically. The tracker "
            "will populate as scans accumulate."
        )
        if st.button("Close", use_container_width=True, key="cohort_close_empty"):
            st.rerun()
        return

    summary = cohort_summary()
    stats   = _rollup_stats(df)

    # Custom HTML tiles instead of st.metric — Streamlit's metric widget
    # renders its help tooltip as a persistent blue dot icon, which the user
    # wanted removed while keeping hover functionality. This CSS pattern
    # (data-tooltip attribute + ::after hover) gives us clean hover-only
    # tooltips with no visible icon at all.
    css = """
<style>
.ct-grid {
    display: grid;
    grid-template-columns: repeat(var(--ct-cols, 5), 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.6rem 0;
}
.ct-card {
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: left;
    position: relative;
}
.ct-card[data-tooltip] { cursor: help; }
.ct-card[data-tooltip]:hover {
    border-color: #3B82F6;
    background: #131826;
}
.ct-card[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: absolute;
    bottom: calc(100% + 10px);
    left: 50%;
    transform: translateX(-50%);
    background: #131826;
    color: #E5E7EB;
    padding: 0.7rem 0.85rem;
    border-radius: 6px;
    border: 1px solid #3B82F6;
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 0.75rem;
    font-weight: 400;
    line-height: 1.55;
    white-space: normal;
    width: 320px;
    text-align: left;
    z-index: 1000;
    box-shadow: 0 6px 18px rgba(0,0,0,0.55);
    pointer-events: none;
}
.ct-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.ct-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
/* Delta indicator (vs previous scan). Added 2026-07-22 so the user can
   see at a glance whether the current tile is better/worse than the last
   time they looked, without needing to open the trend chart. */
.ct-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    margin-top: 0.35rem;
    font-weight: 600;
    letter-spacing: 0.02em;
}
.ct-delta-up   { color: #26D97F; }
.ct-delta-down { color: #FF4D6D; }
.ct-delta-flat { color: #9CA3AF; }
</style>
"""

    def _tile(label: str, value: str, tooltip: Optional[str] = None,
              delta: Optional[str] = None, delta_class: str = "ct-delta-flat") -> str:
        """Render one tile. delta is a pre-formatted string like '↑ +2.1 vs 07-24'
        or None to skip. delta_class picks the color (up/down/flat)."""
        attr = f' data-tooltip="{tooltip}"' if tooltip else ""
        delta_html = (
            f"<div class='ct-delta {delta_class}'>{delta}</div>" if delta else ""
        )
        return (
            f"<div class='ct-card'{attr}>"
            f"<div class='ct-label'>{label}</div>"
            f"<div class='ct-value'>{value}</div>"
            f"{delta_html}"
            f"</div>"
        )

    # Tooltip copy — kept as short plain-text strings so they render cleanly
    # inside the CSS ::after pseudo-element.
    tip_decided = (
        "Number of BUY ZONE flags where the outcome has been resolved as either "
        "a win or a loss. Two conditions must be met for a flag to be 'decided': "
        "(1) the price must have first CLEARED THE PIVOT — no pivot breach means "
        "no trade was ever entered, so the flag stays undecided regardless of "
        "what happens later. (2) After pivot clears, the price must hit either "
        "+2R (2× initial risk from pivot) or -1R (stop level) — whichever comes "
        "first. Flags that haven't triggered OR are still in-flight are NOT "
        "counted. Hit Rate uses ONLY decided flags."
    )
    tip_hit_rate = (
        "% of DECIDED flags that reached +2R BEFORE hitting -1R. Uses flagged "
        "pivot and stop as the R anchor. Only counts flags where the pivot was "
        "actually cleared (a real trade entry) AND the outcome resolved. "
        "Excludes flags that never triggered a trade and flags still in-flight. "
        "Only statistically meaningful once decided count is 30+."
    )
    tip_reached_5 = (
        "% of MATURE flags where a REALIZABLE trade would have captured +5% "
        "above the flagged close. STRICT rules: (1) pivot must have cleared "
        "first — if the stock never triggered entry, the flag is DISQUALIFIED. "
        "(2) after pivot clears, if any close hit the stop BEFORE reaching "
        "+5%, the flag is DISQUALIFIED — the trader would have been stopped "
        "out first. Freshly-flagged tickers with fewer than "
        f"{MATURITY_TRADING_DAYS} trading sessions are EXCLUDED from both "
        "numerator and denominator. This is 'would this trade have worked?', "
        "not raw cohort momentum."
    )
    tip_reached_10 = (
        "% of MATURE flags where a REALIZABLE trade would have captured +10% "
        "above the flagged close. Same STRICT rules as +5%: requires pivot "
        "clear before entry, and disqualifies any flag stopped out before "
        f"reaching the target. Mature flags only (≥{MATURITY_TRADING_DAYS} "
        "trading sessions since first flag)."
    )
    tip_avg_20d = (
        "Average return 20 trading sessions after the flag, across all flags "
        "that have at least 20 sessions of history since being flagged. Newer "
        "flags with less than 20 sessions of forward history are excluded."
    )

    # Header row — 5 tiles
    tiles_html = "".join([
        _tile("TOTAL FLAGS",    str(summary["total"])),
        _tile("UNIQUE TICKERS", str(summary["unique_tickers"])),
        _tile("OLDEST FLAG",    summary["oldest"] or "—"),
        _tile("NEWEST FLAG",    summary["newest"] or "—"),
        _tile("DECIDED",        str(stats.get("n_decided", 0)), tooltip=tip_decided),
    ])
    st.markdown(css + f"<div class='ct-grid' style='--ct-cols:5'>{tiles_html}</div>",
                unsafe_allow_html=True)

    st.markdown("### Cohort performance")

    # Performance row — 4 tiles. REACHED +5% and REACHED +10% are computed
    # only over the MATURE cohort (flags with ≥MATURITY_TRADING_DAYS sessions
    # of forward history). Tile labels reflect the mature-only denominator
    # so the number can't be misread as "% of all flags".
    hr = stats.get("hit_rate_2r")
    max5 = stats.get("hit_max_5pct")
    max10 = stats.get("hit_max_10pct")
    avg20 = stats.get("avg_20d")
    n_mature = stats.get("n_mature", 0) or 0
    n_developing = stats.get("n_developing", 0) or 0

    # ---- Snapshot + delta computation (added 2026-07-22) ----
    # Every dialog open appends a snapshot to cohort_history.jsonl. We then
    # look up the most recent snapshot BEFORE today to compute the delta
    # shown on each hit-rate tile ("↑ +2.1 vs 07-24"). Silent no-op on
    # first-ever open — delta just doesn't render.
    from datetime import datetime as _dt_now
    _today_str = _dt_now.utcnow().strftime("%Y-%m-%d")
    append_snapshot(stats)  # write current
    _prev = get_previous_snapshot(_today_str)  # previous distinct-day snapshot

    def _delta_for(prev_val, cur_val):
        """Return (delta_string, css_class) or (None, None) if no comparison possible."""
        if prev_val is None or cur_val is None:
            return None, None
        try:
            diff = round(float(cur_val) - float(prev_val), 1)
        except (TypeError, ValueError):
            return None, None
        prev_date = (_prev or {}).get("date", "")
        # Show as "MM-DD" for compactness
        prev_short = prev_date[5:] if len(prev_date) >= 10 else prev_date
        if diff > 0:
            return f"↑ +{diff:.1f} vs {prev_short}", "ct-delta-up"
        if diff < 0:
            return f"↓ {diff:.1f} vs {prev_short}", "ct-delta-down"
        return f"→ flat vs {prev_short}", "ct-delta-flat"

    hr_delta,   hr_cls   = _delta_for((_prev or {}).get("hit_rate_2r"),   hr)
    max5_delta, max5_cls = _delta_for((_prev or {}).get("hit_max_5pct"),  max5)
    max10_delta, max10_cls = _delta_for((_prev or {}).get("hit_max_10pct"), max10)

    perf_html = "".join([
        _tile("HIT RATE (+2R B/F -1R)",
              f"{hr}%" if hr is not None else "—",
              tooltip=tip_hit_rate,
              delta=hr_delta, delta_class=hr_cls or "ct-delta-flat"),
        _tile(f"REACHED +5% (of {n_mature} mature)",
              f"{max5}%" if max5 is not None else "—",
              tooltip=tip_reached_5,
              delta=max5_delta, delta_class=max5_cls or "ct-delta-flat"),
        _tile(f"REACHED +10% (of {n_mature} mature)",
              f"{max10}%" if max10 is not None else "—",
              tooltip=tip_reached_10,
              delta=max10_delta, delta_class=max10_cls or "ct-delta-flat"),
        _tile("AVG 20D RETURN",
              f"{avg20:+.2f}%" if avg20 is not None else "—",
              tooltip=tip_avg_20d),
    ])
    st.markdown(f"<div class='ct-grid' style='--ct-cols:4'>{perf_html}</div>",
                unsafe_allow_html=True)

    # Explainer line about the maturity split — shown below the tiles so the
    # user always knows the pipeline is being tracked but the young flags
    # aren't counting against the hit rates.
    muted_color = PALETTE["text_muted"]
    if n_developing > 0:
        st.markdown(
            f"<div style='color:{muted_color};font-size:0.78rem;"
            f"margin-top:0.4rem;font-family:JetBrains Mono'>"
            f"📥 {n_developing} developing — flagged within the last "
            f"{MATURITY_TRADING_DAYS} trading sessions, too young to judge. "
            f"Included in the table below but excluded from the hit-rate math."
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='color:{muted_color};font-size:0.78rem;"
            f"margin-top:0.4rem;font-family:JetBrains Mono'>"
            f"All {n_mature} cohort flags have ≥{MATURITY_TRADING_DAYS} "
            f"trading sessions of forward history."
            f"</div>",
            unsafe_allow_html=True,
        )

    # ---- Milestone drill-downs: which stocks reached +5% / +10% ----
    # User request 2026-07-25: click-through on those tiles so I can go
    # back and study what worked and what didn't. Rendered as expanders
    # right below the tiles so they're one click away.
    #
    # Only shows tickers that ACTUALLY reached the milestone (date is
    # non-null). Sort by days-to-milestone ascending so the fastest
    # movers surface first — those are the best pattern studies.
    reached_5_df  = df[df["Reached 5%"].notna()].copy() if "Reached 5%" in df.columns else pd.DataFrame()
    reached_10_df = df[df["Reached 10%"].notna()].copy() if "Reached 10%" in df.columns else pd.DataFrame()

    def _build_milestone_table(source_df: pd.DataFrame, milestone_col: str, days_col: str) -> pd.DataFrame:
        """Slice + sort the cohort for a milestone drill-down."""
        if source_df.empty:
            return source_df
        cols_to_keep = ["Ticker", "Flag Date", milestone_col, days_col,
                        "Max %", "Return %", "Sector", "Signal", "VCP"]
        cols_present = [c for c in cols_to_keep if c in source_df.columns]
        out = source_df[cols_present].copy()
        # Rename for clarity in the drill-down table
        rename_map = {
            milestone_col: "Reached On",
            days_col:      "Days to Reach",
            "Flag Date":   "First Flagged",
            "Signal":      "Regime When Flagged",
        }
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
        if "Days to Reach" in out.columns:
            out = out.sort_values("Days to Reach", ascending=True)
        return out.reset_index(drop=True)

    with st.expander(
        f"📈 Show {len(reached_5_df)} stocks that reached +5% "
        f"(click to study what worked)", expanded=False
    ):
        table_5 = _build_milestone_table(reached_5_df, "Reached 5%", "Days→5%")
        if table_5.empty:
            st.caption("No cohort members have reached +5% yet.")
        else:
            st.caption(
                "**Strict rules:** pivot must have cleared AND stop not hit "
                "before target. Sorted by fastest to reach the milestone. **First Flagged** = "
                "the date this ticker first appeared on the BUY ZONE list. "
                "**Reached On** = the first session where the close hit "
                "+5% above the flagged close. **Days to Reach** = trading "
                "sessions between the two (0 = same day)."
            )
            st.dataframe(
                table_5, use_container_width=True, hide_index=True,
                height=min(400, 80 + len(table_5) * 36),
            )

    with st.expander(
        f"🚀 Show {len(reached_10_df)} stocks that reached +10% "
        f"(click to study the big winners)", expanded=False
    ):
        table_10 = _build_milestone_table(reached_10_df, "Reached 10%", "Days→10%")
        if table_10.empty:
            st.caption("No cohort members have reached +10% yet.")
        else:
            st.caption(
                "The setups that produced real profit. Study these charts "
                "closely — they show what an A+ setup actually looks like "
                "before it works. Sorted by fastest to reach +10%."
            )
            st.dataframe(
                table_10, use_container_width=True, hide_index=True,
                height=min(400, 80 + len(table_10) * 36),
            )

    # ---- Hit Rate drill-down: WINS and LOSSES side of the +2R tile ----
    # Complements the +5% / +10% tables above. This shows the DECIDED
    # flags (pivot cleared, race between +2R and -1R resolved) so the
    # user can study which setups paid off vs which got stopped out.
    #
    # Two expanders — same structure as the milestone tables above:
    #   🏆 WINS   = hit_2r_before_neg_1r is True  (Worked? = "✓")
    #   💥 LOSSES = hit_2r_before_neg_1r is False (Worked? = "✗")
    #
    # Undecided ("…") flags — pivot never cleared or trade still in
    # flight — are excluded because they're not yet part of the hit rate.
    wins_df   = df[df["Worked?"] == "✓"].copy() if "Worked?" in df.columns else pd.DataFrame()
    losses_df = df[df["Worked?"] == "✗"].copy() if "Worked?" in df.columns else pd.DataFrame()

    def _build_hitrate_table(source_df: pd.DataFrame) -> pd.DataFrame:
        """Slice + sort the cohort for a hit-rate drill-down.
        Sorted by Max % descending so the biggest movers surface first
        (for wins that's the runners; for losses it's the ones that
        looked good before rolling over — most instructive)."""
        if source_df.empty:
            return source_df
        cols_to_keep = ["Ticker", "Flag Date", "TDays", "Return %", "Max %",
                        "MaxDD %", "Sector", "Signal", "VCP"]
        cols_present = [c for c in cols_to_keep if c in source_df.columns]
        out = source_df[cols_present].copy()
        rename_map = {
            "Flag Date":  "First Flagged",
            "TDays":      "Trading Days",
            "Signal":     "Regime When Flagged",
        }
        out = out.rename(columns={k: v for k, v in rename_map.items() if k in out.columns})
        if "Max %" in out.columns:
            out = out.sort_values("Max %", ascending=False)
        return out.reset_index(drop=True)

    with st.expander(
        f"🏆 Show {len(wins_df)} WINS — trades that hit +2R before -1R "
        f"(the payoff studies)", expanded=False
    ):
        table_wins = _build_hitrate_table(wins_df)
        if table_wins.empty:
            st.caption(
                "No decided wins yet. A 'win' requires the pivot to clear "
                "AND the trade to reach +2R above the pivot before dropping "
                "-1R below it. Fresh flags where the pivot hasn't cleared "
                "yet stay in the '…' bucket and don't count either way."
            )
        else:
            st.caption(
                "**Decided wins:** pivot cleared and price reached +2R above "
                "pivot before falling -1R below it. Sorted by **Max %** "
                "descending — the biggest runners at the top. Study these "
                "charts to internalize what a working setup looks like "
                "before and during the move."
            )
            st.dataframe(
                table_wins, use_container_width=True, hide_index=True,
                height=min(400, 80 + len(table_wins) * 36),
            )

    with st.expander(
        f"💥 Show {len(losses_df)} LOSSES — trades that hit -1R before +2R "
        f"(the failure studies)", expanded=False
    ):
        table_losses = _build_hitrate_table(losses_df)
        if table_losses.empty:
            st.caption(
                "No decided losses yet. A 'loss' requires the pivot to have "
                "cleared AND price to have fallen -1R below the pivot before "
                "reaching +2R above it."
            )
        else:
            st.caption(
                "**Decided losses:** pivot cleared but price rolled over and "
                "hit -1R before making +2R. Sorted by **Max %** descending — "
                "the ones that pushed the furthest into profit before failing "
                "are the most valuable studies. Ask: was the exit signal "
                "there before the stop hit? Did I miss a warning?"
            )
            st.dataframe(
                table_losses, use_container_width=True, hide_index=True,
                height=min(400, 80 + len(table_losses) * 36),
            )

    # ---- Trend chart of hit rates over time ----
    # Line chart of the last ~60 days of REACHED +5%, +10%, and 2R hit rate.
    # Uses plotly (not st.line_chart) so we can force DATE-only x-axis
    # ticks — Streamlit's built-in chart was showing hourly labels
    # (09 AM, 03 PM, etc.) because it auto-formatted based on a short
    # date range (user report 2026-07-25). Plotly with tickformat="%b %d"
    # and dtick="D1" gives a clean one-tick-per-day day-month axis.
    _hist = load_history(days=60)
    if len(_hist) >= 2:
        import pandas as pd
        try:
            import plotly.graph_objects as go
            chart_df = pd.DataFrame([
                {
                    "Date":         pd.to_datetime(h.get("date")),
                    "Reached +5%":  h.get("hit_max_5pct"),
                    "Reached +10%": h.get("hit_max_10pct"),
                    "Hit Rate 2R":  h.get("hit_rate_2r"),
                }
                for h in _hist
            ])
            st.markdown("### Hit-rate trend (last 60 days)")
            st.caption(
                "Each point is one scan day's cohort snapshot. **Rising** = the "
                "regime is starting to pay; **flat** = still developing; "
                "**falling** = setups deteriorating (tighten stops, stop adding). "
                "Missing days = no scan was run."
            )

            fig = go.Figure()
            _COLORS = {
                "Reached +5%":  "#EF4444",  # red — matches user's SMA 200 red
                "Reached +10%": "#3B82F6",  # blue
                "Hit Rate 2R":  "#22C55E",  # green
            }
            for col in ("Reached +5%", "Reached +10%", "Hit Rate 2R"):
                fig.add_trace(go.Scatter(
                    x=chart_df["Date"], y=chart_df[col],
                    mode="lines+markers", name=col,
                    line=dict(color=_COLORS[col], width=2.2),
                    marker=dict(size=6),
                    hovertemplate=f"{col}: %{{y:.1f}}%<br>%{{x|%b %d %Y}}<extra></extra>",
                ))
            # All text forced to #FFFFFF (pure white). Plotly-dark's default
            # font color is ~#F0F0F0 which reads as dim grey against the
            # near-black chart background (user report 2026-07-25).
            _WHITE = "#FFFFFF"
            fig.update_layout(
                template="plotly_dark",
                plot_bgcolor="#0F1620",
                paper_bgcolor="#0F1620",
                font=dict(family="JetBrains Mono, monospace", size=12,
                          color=_WHITE),
                height=320,
                margin=dict(l=50, r=20, t=20, b=40),
                xaxis=dict(
                    # Day-level ticks only — no hour/minute labels
                    tickformat="%b %d",
                    dtick=86400000.0,  # one day in milliseconds
                    gridcolor="#1F2937",
                    tickangle=-30,
                    tickfont=dict(color=_WHITE, size=12),
                    color=_WHITE,
                ),
                yaxis=dict(
                    title=dict(text="%", font=dict(color=_WHITE, size=12)),
                    gridcolor="#1F2937",
                    ticksuffix="%",
                    tickfont=dict(color=_WHITE, size=12),
                    color=_WHITE,
                ),
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)",
                    font=dict(color=_WHITE, size=13),
                ),
                hoverlabel=dict(
                    bgcolor="#0F1620", bordercolor="#00D4FF",
                    font=dict(color=_WHITE),
                ),
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass  # never let chart failure block the rest of the dialog
    else:
        st.markdown("### Hit-rate trend")
        st.caption(
            f"📈 Trend chart appears once you have 2+ daily snapshots "
            f"logged ({len(_hist)} so far). Open this dialog after your "
            f"next scan to start building history."
        )

    st.markdown("### Every BUY ZONE flag + forward returns")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=min(500, 90 + len(df) * 36),
        column_config={
            "Ticker":    st.column_config.TextColumn("Ticker", pinned=True, width="small"),
            "Flag Date": st.column_config.TextColumn("Flag Date", width="small"),
            "Sector":    st.column_config.TextColumn("Sector", width="small"),
            "Signal":    st.column_config.TextColumn("Regime", width="small",
                help="Market signal on the flag date (RED / GREEN / YELLOW)."),
            "Flag $":    st.column_config.NumberColumn("Flag $", format="$%.2f", width="small"),
            "Now $":     st.column_config.NumberColumn("Now $", format="$%.2f", width="small"),
            "Times Flagged": st.column_config.NumberColumn(
                "Flags", format="%d", width="small",
                help="How many times this ticker has appeared on the BUY ZONE list. "
                     "The cohort dedupes by ticker (only earliest flag counted), but "
                     "this column shows the raw re-flag count for transparency. "
                     "Values >1 mean the setup persisted across multiple scans."),
            "Days":      st.column_config.NumberColumn("Days", format="%d", width="small",
                help="Calendar days since the FIRST flag of this ticker."),
            "TDays":     st.column_config.NumberColumn("TDays", format="%d", width="small",
                help=f"Trading sessions of forward price history since first flag. "
                     f"Flags with TDays ≥ {MATURITY_TRADING_DAYS} are 'mature' and "
                     f"count in the REACHED +5% / +10% math above. Younger flags "
                     f"are excluded so freshly-flagged tickers don't drag the ratio "
                     f"down before they've had a chance to move."),
            "Return %":  st.column_config.NumberColumn("Return %", format="%+.2f%%", width="small",
                help="Current return vs. flagged close."),
            "Max %":     st.column_config.NumberColumn("Max %", format="%+.2f%%", width="small",
                help="Maximum favorable return achieved at any point post-flag."),
            "MaxDD %":   st.column_config.NumberColumn("MaxDD %", format="%+.2f%%", width="small",
                help="Maximum drawdown (adverse move) from flagged close at any point post-flag."),
            "5d %":      st.column_config.NumberColumn("5d %",  format="%+.2f%%", width="small",
                help="Return 5 trading sessions after the flag."),
            "10d %":     st.column_config.NumberColumn("10d %", format="%+.2f%%", width="small",
                help="Return 10 trading sessions after the flag."),
            "20d %":     st.column_config.NumberColumn("20d %", format="%+.2f%%", width="small",
                help="Return 20 trading sessions after the flag."),
            "Worked?":   st.column_config.TextColumn("Worked?", width="small",
                help="Trade outcome using pivot and stop as R anchors. ✓ = pivot "
                     "was cleared (entry triggered) AND price then hit +2R before "
                     "-1R. ✗ = pivot cleared AND price then hit -1R before +2R. "
                     "… = pivot never cleared (no trade ever entered) OR pivot "
                     "cleared but neither +2R nor -1R hit yet (still in-flight)."),
            "VCP":       st.column_config.TextColumn("VCP", width="small"),
            "Pressure":  st.column_config.NumberColumn("Pressure", format="%.2f", width="small"),
            "% > 50MA":  st.column_config.NumberColumn("%>50MA", format="%+.1f%%", width="small"),
        },
    )

    st.markdown("### Sector composition")
    st.caption(
        "A leadership tell — if BUY ZONE flags are concentrated in defensive "
        "sectors (utilities, staples, REITs), that's itself a bearish regime "
        "signal. Growth/tech/small-cap concentration = risk-on confirmation."
    )
    sec_df = _sector_composition(df)
    if not sec_df.empty:
        # Equal-width tile grid — CSS Grid with a fixed column count so every
        # tile is the SAME width (wide enough to comfortably fit the longest
        # sector name, "Communication Services"). Column count picked to
        # prefer a 2-row layout; falls back to 3 rows only when >8 sectors,
        # per user request 2026-07-22.
        n_sectors = len(sec_df)
        if n_sectors <= 3:
            # 1 row, one column per sector
            n_cols = n_sectors
        elif n_sectors <= 6:
            # 3 cols × 2 rows — comfortable width for full sector names
            n_cols = 3
        elif n_sectors <= 8:
            # 4 cols × 2 rows — tighter but still fits
            n_cols = 4
        else:
            # 4 cols × 3 rows — better to widen tiles and add a row than
            # cram 5-6 per row where "Communication Services" would clip
            n_cols = 4

        tiles_css = f"""
<style>
.sec-grid {{
    display: grid;
    grid-template-columns: repeat({n_cols}, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.6rem 0;
}}
.sec-tile {{
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.6rem 0.5rem;
    text-align: center;
}}
.sec-tile-name {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.03em;
    margin-bottom: 0.35rem;
    line-height: 1.25;
    /* Prevent wrapping on shorter names so tile heights stay consistent */
    min-height: 1.8rem;
    display: flex;
    align-items: center;
    justify-content: center;
}}
.sec-tile-count {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.35rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1;
}}
</style>
"""
        tile_html = "".join(
            f"<div class='sec-tile'>"
            f"<div class='sec-tile-name'>{row['Sector']}</div>"
            f"<div class='sec-tile-count'>{int(row['Flags'])}</div>"
            f"</div>"
            for _, row in sec_df.iterrows()
        )
        st.markdown(
            tiles_css + f"<div class='sec-grid'>{tile_html}</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("No sector data available yet.")

    if st.button("Close", use_container_width=True, key="cohort_close"):
        st.rerun()
