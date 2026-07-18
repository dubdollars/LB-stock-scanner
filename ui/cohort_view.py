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
from scanner.data_cache import load_price_history
from ui.style import PALETTE


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
        "max_return_pct":        None,
        "max_drawdown_pct":      None,
        "hit_2r_before_neg_1r":  None,
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

    return {
        "current_close":         round(current_close, 2),
        "current_return_pct":    current_return_pct,
        "days_since":            days_since,
        "max_return_pct":        max_return_pct,
        "max_drawdown_pct":      max_drawdown_pct,
        "hit_2r_before_neg_1r":  hit_2r,
        "close_5d":              round(close_5d, 2) if close_5d else None,
        "close_10d":             round(close_10d, 2) if close_10d else None,
        "close_20d":             round(close_20d, 2) if close_20d else None,
        "return_5d_pct":         _pct(close_5d),
        "return_10d_pct":        _pct(close_10d),
        "return_20d_pct":        _pct(close_20d),
    }


def _build_cohort_df() -> pd.DataFrame:
    """Load cohort log + enrich with forward-return metrics. Newest first."""
    entries = load_cohort()
    if not entries:
        return pd.DataFrame()
    rows = []
    for e in entries:
        fwd = _compute_forward_metrics(e)
        rows.append({
            "Flag Date":      e.get("flag_date"),
            "Ticker":         e.get("ticker"),
            "Sector":         e.get("flag_sector"),
            "Signal":         e.get("flag_market_signal"),
            "Flag $":         e.get("flag_close"),
            "Now $":          fwd.get("current_close"),
            "Days":           fwd.get("days_since"),
            "Return %":       fwd.get("current_return_pct"),
            "Max %":          fwd.get("max_return_pct"),
            "MaxDD %":        fwd.get("max_drawdown_pct"),
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
    return pd.DataFrame(rows)


def _rollup_stats(df: pd.DataFrame) -> dict:
    """Cohort-level rollup: hit rates + avg returns."""
    if df.empty:
        return {}

    # Only count decided outcomes for hit rate (ignore "…" still-in-flight).
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

    # Hit rate at +1R (using return %, no pivot/stop needed)
    # +1R proxy: return_5d_pct >= 5% (rough proxy for +1R since we don't have
    # per-row R denominator readily). Better: check current return >= 5%.
    # We'll keep it directional and just report the +5% and +10% hit rates.
    def _hit_rate_at(pct: float, col: str = "Max %") -> Optional[float]:
        s = df[col].dropna()
        if s.empty:
            return None
        return round((s >= pct).sum() / len(s) * 100, 1)

    return {
        "n_total":       len(df),
        "n_decided":     n_dec,
        "hit_rate_2r":   round(hit_rate_2r, 1) if hit_rate_2r is not None else None,
        "hit_max_5pct":  _hit_rate_at(5,  "Max %"),
        "hit_max_10pct": _hit_rate_at(10, "Max %"),
        "avg_5d":        _avg("5d %"),
        "avg_10d":       _avg("10d %"),
        "avg_20d":       _avg("20d %"),
        "avg_current":   _avg("Return %"),
    }


def _sector_composition(df: pd.DataFrame) -> pd.DataFrame:
    """Count of flags per sector — used as a regime-tell."""
    if df.empty or "Sector" not in df.columns:
        return pd.DataFrame()
    counts = df["Sector"].fillna("N/A").value_counts().reset_index()
    counts.columns = ["Sector", "Flags"]
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
</style>
"""

    def _tile(label: str, value: str, tooltip: Optional[str] = None) -> str:
        attr = f' data-tooltip="{tooltip}"' if tooltip else ""
        return (
            f"<div class='ct-card'{attr}>"
            f"<div class='ct-label'>{label}</div>"
            f"<div class='ct-value'>{value}</div>"
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
        "% of flags whose maximum post-flag price reached at least +5% above the "
        "flagged close, at any point since the flag date. Includes flags that "
        "later gave back the gain — this is 'ever reached', not 'still up'."
    )
    tip_reached_10 = (
        "% of flags whose maximum post-flag price reached at least +10% above the "
        "flagged close, at any point since the flag date. Same rules as +5% — "
        "'ever reached', not 'still up'."
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

    # Performance row — 4 tiles
    hr = stats.get("hit_rate_2r")
    max5 = stats.get("hit_max_5pct")
    max10 = stats.get("hit_max_10pct")
    avg20 = stats.get("avg_20d")
    perf_html = "".join([
        _tile("HIT RATE (+2R B/F -1R)",
              f"{hr}%" if hr is not None else "—",
              tooltip=tip_hit_rate),
        _tile("REACHED +5%",
              f"{max5}%" if max5 is not None else "—",
              tooltip=tip_reached_5),
        _tile("REACHED +10%",
              f"{max10}%" if max10 is not None else "—",
              tooltip=tip_reached_10),
        _tile("AVG 20D RETURN",
              f"{avg20:+.2f}%" if avg20 is not None else "—",
              tooltip=tip_avg_20d),
    ])
    st.markdown(f"<div class='ct-grid' style='--ct-cols:4'>{perf_html}</div>",
                unsafe_allow_html=True)

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
            "Days":      st.column_config.NumberColumn("Days", format="%d", width="small",
                help="Calendar days since the flag."),
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
    st.dataframe(sec_df, use_container_width=True, hide_index=True, height=250)

    if st.button("Close", use_container_width=True, key="cohort_close"):
        st.rerun()
