"""
MOVERS page — the reverse funnel view (added 2026-08-07).

Answers: "which stocks are the market actually accumulating right now?"
Filter starts from confirmed demand (gain% + volume) and shows structure
context alongside. Complements the Momentum page (structure-first) rather
than replacing it.

Data source
-----------
Reads the `movers` list embedded in the most recent healthy scan
(latest.json). Populated by scanner.movers.find_movers, computed as
part of every full scan. No separate scan invocation needed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import streamlit as st

from scanner.cache import load_latest_scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_millions(vol: Optional[float]) -> str:
    if vol is None or vol <= 0:
        return "-"
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    return f"{int(vol)}"


def _fmt_dollars(v: Optional[float]) -> str:
    if v is None or v <= 0:
        return "-"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${int(v)}"


def _movers_dataframe(movers: list[dict]) -> pd.DataFrame:
    """Convert raw movers list into a display DataFrame with derived columns."""
    if not movers:
        return pd.DataFrame()
    df = pd.DataFrame(movers)
    # Formatted volume columns for display
    if "Vol_Today" in df.columns:
        df["Vol Today"] = df["Vol_Today"].apply(_fmt_millions)
    if "Avg_Vol_50" in df.columns:
        df["Avg Vol (50d)"] = df["Avg_Vol_50"].apply(_fmt_millions)
    if "Avg_Dollar_Volume_20d" in df.columns:
        df["ADV $"] = df["Avg_Dollar_Volume_20d"].apply(_fmt_dollars)
    # In_Buy_Zone as an emoji indicator
    if "In_Buy_Zone" in df.columns:
        df["BZ"] = df["In_Buy_Zone"].apply(lambda x: "●" if x else "")
    if "In_Momentum_Scan" in df.columns:
        df["In MOM"] = df["In_Momentum_Scan"].apply(lambda x: "●" if x else "")
    # Tradeability indicator — green dot if ≥ $20M ADV, red if not.
    # Renders even when the tradeable toggle is off, so the user can visually
    # sort by eligibility at a glance instead of squinting at ADV dollars.
    if "Is_Tradeable" in df.columns:
        df["Trade"] = df["Is_Tradeable"].apply(lambda x: "🟢" if x else "🔴")
    return df


# ---------------------------------------------------------------------------
# TOC dialog
# ---------------------------------------------------------------------------

@st.dialog("MOVERS — how it works")
def _movers_toc_dialog():
    st.markdown(
        """
### What MOVERS is

The **reverse funnel**. Momentum scans start from structure (VCP, base,
RS character) and hope demand shows up. MOVERS starts from **confirmed
demand today** and shows structure context alongside. This is how you
find the leaders the tape is picking — not the ones you'd prefer it to
pick.

### The four gates

1. **Today's close ≥ prior close × 1.03** — real move, not noise
2. **Today's volume ≥ 1.5x robust median 50-day** — institutional footprint
   *(Robust median drops the top 5% of days so earnings spikes don't
   inflate the denominator.)*
3. **Trend template intact** — price above 50/150/200 MAs, MAs stacked,
   200MA rising, RS ≥ 70. No falling knives.
4. **Close in upper half of today's range** — held the move to the bell

### The two ranking scores

- **Quality Score (default sort)** = `(Gain % / ATR %) × Vol Ratio`.
  Normalizes gain by the stock's typical daily volatility so mid-caps
  aren't crushed by micro-cap squeezes. A $50 mid-cap with 3% ATR gapping
  6% moved 2 typical days; a $5 micro-cap with 20% ATR gapping 20% only
  moved 1 typical day. The mid-cap deserves the higher rank — and
  Quality Score gives it that.
- **Move Score (raw)** = `Gain % × Vol Ratio`. The pure excitement view.
  Structurally favors lottery tickets because it doesn't normalize for
  volatility. Available via the Sort dropdown when you specifically
  want to see the biggest raw movers.

### How to use it

- **`Trade` column (🟢/🔴)** — 🟢 means the stock has 20-day average
  dollar volume ≥ $20M (tradeable at typical account sizes). 🔴 means
  it's a thin micro-cap where your entry would move the tape and your
  exit would blow the spread. The "Tradeable only" toggle above the
  table defaults ON — you have to explicitly opt into seeing the 🔴s.
- **`In MOM` column** — is this ticker in tonight's Momentum funnel too?
  If yes, cross-reference the Momentum table for VCP grade, buy zone
  status, etc. If NO, this is a name your structural filter is
  excluding — worth an honest look before you dismiss it.
- **`BZ` column** — if the momentum scan also flagged it as BUY ZONE,
  you have BOTH confirmed demand today AND anticipatory setup structure.
  Highest-conviction combo.
- **No industry filter by default.** Intentional — the whole point is to
  see leadership your normal filters might exclude. If refiners /
  energy / financials are ripping while your industry filter points at
  semis, MOVERS is where you find out.

### What MOVERS does NOT do

- It does not tell you WHEN to enter. It shows you what moved today.
- It does not gate on catalyst quality. A 3%+ move on 1.5x vol could
  be earnings, could be a squeeze, could be real institutional flow.
  Check the chart before assuming.
- It does not respect your BUY ZONE discipline. Some of these names are
  extended, some just gapped up. Pull the chart, don't chase.

### Complement, not replacement

Use MOVERS ALONGSIDE Momentum:
- **Momentum** = "what's ready to move" (anticipatory)
- **MOVERS** = "what just moved on demand" (confirmatory)
- **Top Stocks** = intersection (best of both worlds)
        """
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_movers_sidebar(scan: Optional[dict]):
    with st.sidebar:
        st.markdown("### MOVERS")
        movers = (scan or {}).get("movers") or []
        st.caption(f"Latest scan: {len((scan or {}).get('candidates') or [])} momentum candidates · {len(movers)} movers")
        if scan and scan.get("timestamp"):
            st.caption(f"Timestamp: {scan['timestamp'][:19]}")

        st.markdown("---")
        if st.button("📖 About MOVERS", use_container_width=True, key="movers_sidebar_toc"):
            _movers_toc_dialog()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render_movers_page():
    scan = st.session_state.get("latest_scan") or load_latest_scan()
    if not scan:
        st.warning("No scan data available. Run a Momentum scan first — MOVERS is computed alongside it.")
        _render_movers_sidebar(None)
        return

    _render_movers_sidebar(scan)

    movers = scan.get("movers") or []

    # Header + summary strip
    st.markdown("## 🚀 MOVERS — Demand Today")
    st.caption(
        "Reverse funnel — names moving ≥3% today on ≥1.5× typical volume with trend template intact. "
        "Ranked by move × volume."
    )

    if not movers:
        st.info(
            "No movers found in the most recent scan. This can mean the tape is quiet, "
            "the scan pre-dates the MOVERS feature (run a fresh scan to populate), or "
            "market breadth is thin. Cross-check with the Control Tower breadth tiles on Momentum."
        )
        return

    df = _movers_dataframe(movers)

    # Tiles: how many movers, how many overlap with momentum, avg vol ratio
    total = len(df)
    n_in_mom = int(df["In_Momentum_Scan"].sum()) if "In_Momentum_Scan" in df.columns else 0
    n_in_bz = int(df["In_Buy_Zone"].sum()) if "In_Buy_Zone" in df.columns else 0
    avg_vol_ratio = float(df["Vol_Ratio"].mean()) if "Vol_Ratio" in df.columns else 0.0
    avg_gain = float(df["Gain_Pct"].mean()) if "Gain_Pct" in df.columns else 0.0

    tcol1, tcol2, tcol3, tcol4 = st.columns(4)
    with tcol1:
        st.metric("Total Movers", f"{total}")
    with tcol2:
        st.metric("Also in Momentum", f"{n_in_mom}",
                  help="Movers that ALSO passed the strict Momentum funnel. Cross-reference for signal quality.")
    with tcol3:
        st.metric("Also in BUY ZONE", f"{n_in_bz}",
                  help="Highest-conviction: confirmed demand today AND anticipatory structure setup.")
    with tcol4:
        st.metric("Avg Move / Vol", f"{avg_gain:.1f}% · {avg_vol_ratio:.1f}×",
                  help="Average gain % and volume ratio across all movers tonight.")

    st.markdown("---")

    # Filter row 1 — Index + Search + Sort selector
    fcol1, fcol2, fcol3 = st.columns([2, 2, 3])
    with fcol1:
        idx_opts = sorted(df["Index"].dropna().unique().tolist()) if "Index" in df.columns else []
        idx_filter = st.multiselect("Index", options=idx_opts, default=None, placeholder="All", key="movers_idx")
    with fcol2:
        sort_mode = st.selectbox(
            "Sort by",
            options=["Quality Score (default)", "Move Score (raw)", "Gain %", "Vol ×", "ADV $"],
            index=0,
            help=(
                "Quality Score = (gain% / ATR%) × vol ratio — normalizes for "
                "volatility so a 5% move in a $60M mid-cap can outrank a 20% "
                "squeeze in a $5M micro-cap. Raw Move Score = gain% × vol ratio "
                "(the pure excitement view; structurally favors lottery tickets)."
            ),
            key="movers_sort_mode",
        )
    with fcol3:
        search = st.text_input("Search", placeholder="e.g. MPC or refiners", key="movers_search")

    # Filter row 2 — two toggles side-by-side
    tcol1, tcol2, _spacer = st.columns([2, 2, 3])
    with tcol1:
        tradeable_only = st.checkbox(
            "Tradeable only ($20M+ ADV)",
            value=True,
            help=(
                "Hide movers whose 20-day average dollar volume is below $20M. "
                "Sub-$20M names (DCTH-class micro-caps) have 6-figure daily "
                "notional — your entry moves the tape and your exit blows the "
                "spread. Toggle OFF to see the raw pool for cross-reference."
            ),
            key="movers_tradeable_only",
        )
    with tcol2:
        overlap_only = st.checkbox(
            "In Momentum funnel only",
            value=False,
            help=(
                "Show only movers that also passed the strict Momentum scan. "
                "Uncheck to see leadership your structural filter is excluding."
            ),
            key="movers_overlap_only",
        )

    filtered = df.copy()
    if idx_filter:
        filtered = filtered[filtered["Index"].isin(idx_filter)]
    if tradeable_only and "Is_Tradeable" in filtered.columns:
        filtered = filtered[filtered["Is_Tradeable"] == True]
    if overlap_only and "In_Momentum_Scan" in filtered.columns:
        filtered = filtered[filtered["In_Momentum_Scan"] == True]
    if search:
        s = search.upper()
        mask = filtered["Ticker"].astype(str).str.upper().str.contains(s, na=False)
        if "Sector" in filtered.columns:
            mask = mask | filtered["Sector"].astype(str).str.upper().str.contains(s, na=False)
        if "Industry" in filtered.columns:
            mask = mask | filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)
        filtered = filtered[mask]

    # Apply selected sort mode (all descending — biggest = best)
    _sort_key_map = {
        "Quality Score (default)": "Quality_Score",
        "Move Score (raw)":        "Move_Score",
        "Gain %":                  "Gain_Pct",
        "Vol ×":                   "Vol_Ratio",
        "ADV $":                   "Avg_Dollar_Volume_20d",
    }
    _sort_col = _sort_key_map.get(sort_mode, "Quality_Score")
    if _sort_col in filtered.columns:
        filtered = filtered.sort_values(_sort_col, ascending=False)

    st.caption(f"Showing {len(filtered)} of {total} movers")

    # Display columns — Trade eligibility + Quality Score up front so the
    # tradeable-vs-untradeable read is instantaneous.
    display_cols = [
        "Ticker", "Trade", "Index", "Gain_Pct", "Vol_Ratio", "Atr_Pct",
        "Quality_Score", "Move_Score",
        "BZ", "In MOM", "RS", "VCP_Quality",
        "Price", "ADV $", "Vol Today", "Avg Vol (50d)", "Close_Pos",
        "Pct_Above_50MA", "Pressure_20d", "Days_To_Earnings",
        "Sector", "Industry", "Industry_Rank", "Market_Cap",
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]

    col_cfg = {
        "Ticker":         st.column_config.TextColumn("Ticker", width="small", pinned=True),
        "Trade":          st.column_config.TextColumn("Trade", width="small",
                                                      help="🟢 = tradeable (20-day ADV ≥ $20M). 🔴 = too thin — your entry moves the tape."),
        "Index":          st.column_config.TextColumn("Idx", width="small"),
        "Gain_Pct":       st.column_config.NumberColumn("Gain %", format="%.2f%%", width="small",
                                                        help="Today's close vs prior close."),
        "Vol_Ratio":      st.column_config.NumberColumn("Vol ×", format="%.2f×", width="small",
                                                        help="Today's volume / robust 50-day median."),
        "Atr_Pct":        st.column_config.NumberColumn("ATR %", format="%.2f%%", width="small",
                                                        help="20-day ATR as % of price. High ATR = volatile stock; low ATR = calm. Feeds the Quality Score normalization."),
        "Quality_Score":  st.column_config.NumberColumn("Quality", format="%.1f", width="small",
                                                        help="ATR-normalized ranking: (Gain % / ATR %) × Vol Ratio. Levels the playing field between low-vol mid-caps and high-vol micro-caps. This is the default sort."),
        "Move_Score":     st.column_config.NumberColumn("Raw", format="%.1f", width="small",
                                                        help="Raw composite: Gain % × Vol Ratio. Pure excitement — structurally favors lottery tickets."),
        "BZ":             st.column_config.TextColumn("BZ", width="small",
                                                      help="Filled dot = also in Momentum BUY ZONE."),
        "In MOM":         st.column_config.TextColumn("MOM", width="small",
                                                      help="Filled dot = also passed the strict Momentum funnel."),
        "RS":             st.column_config.NumberColumn("RS", format="%d", width="small"),
        "VCP_Quality":    st.column_config.TextColumn("VCP", width="small"),
        "Price":          st.column_config.NumberColumn("Price", format="$%.2f", width="small"),
        "ADV $":          st.column_config.TextColumn("ADV $", width="small",
                                                      help="20-day average daily dollar volume. Tradeability floor is $20M."),
        "Vol Today":      st.column_config.TextColumn("Vol Today", width="small"),
        "Avg Vol (50d)":  st.column_config.TextColumn("Avg Vol", width="small"),
        "Close_Pos":      st.column_config.NumberColumn("Close Pos", format="%.2f", width="small",
                                                        help="Where today's close sits in the day's range (0=low, 1=high). Must be ≥ 0.5."),
        "Pct_Above_50MA": st.column_config.NumberColumn("% > 50MA", format="%.1f%%", width="small"),
        "Pressure_20d":   st.column_config.NumberColumn("Pressure", format="%.2f", width="small",
                                                        help="20-day up-vol / down-vol accumulation ratio."),
        "Days_To_Earnings": st.column_config.NumberColumn("Days ER", format="%d", width="small"),
        "Sector":         st.column_config.TextColumn("Sector", width="medium"),
        "Industry":       st.column_config.TextColumn("Industry", width="medium"),
        "Industry_Rank":  st.column_config.NumberColumn("Ind Rank", format="%d", width="small"),
        "Market_Cap":     st.column_config.TextColumn("Mkt Cap", width="small"),
    }

    st.dataframe(
        filtered[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config=col_cfg,
        height=min(700, 60 + 35 * len(filtered)),
    )

    # Small note explaining the ranking
    st.caption(
        "💡 **Quality** (default) = `(Gain / ATR) × Vol Ratio` — normalizes for volatility, "
        "so a 5% move in a $60M mid-cap can outrank a 20% squeeze in a $5M micro. "
        "**Raw** = `Gain × Vol Ratio` (pure excitement view). "
        "🟢 Trade column filters micro-caps you can't execute cleanly at typical account sizes."
    )
