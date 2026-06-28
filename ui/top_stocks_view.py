"""
Top Stocks mode UI — the intersection of the SEPA Long scanner and the
IBD Quality scanner. This is the app's DEFAULT HOME PAGE.

Only the very best stocks show up here: those that pass BOTH the technical
SEPA criteria AND the IBD-elite fundamental criteria.

Wholly separate page renderer + sidebar — if this file is deleted, nothing
else in the app breaks. The Top Stocks button would just 404.
"""
from __future__ import annotations

import re
from datetime import datetime
import pandas as pd
import streamlit as st

from scanner.top_stocks import (
    compute_top_stocks,
    DEFAULT_MIN_COMPOSITE,
    DEFAULT_MIN_EPS,
    DEFAULT_MIN_AD,
    DEFAULT_MIN_UD_VOL,
    DEFAULT_MAX_RSI,
    AD_ORDER,
    FRESHNESS_HOURS,
)
from .style import PALETTE, panel
from .excel_export import format_dataframe_to_excel


# ---------------------------------------------------------------------------
# Hover-card metric tile docs (matches the Long-scan breadth tile pattern)
# ---------------------------------------------------------------------------

_TS_METRIC_DOCS = {
    "MOMENTUM CANDIDATES": (
        "**Stocks that survived the most recent SEPA Momentum scan.**\n\n"
        "These are the technical candidates: passing Minervini's SEPA template "
        "(trend stage 2, RS ≥ threshold, near a buy pivot, etc.).\n\n"
        "Top Stocks always begins with the Momentum scan as the candidate pool — "
        "the elite list cannot exceed this number."
    ),
    "QUALITY UNIVERSE": (
        "**Total stocks rated in the most recent IBD-style Quality scan.**\n\n"
        "Russell 3000 by default — about 3,000-6,000 names depending on coverage. "
        "Every Momentum candidate that also appears here gets fundamental ratings "
        "(Composite, EPS, A/D, U/D Vol).\n\n"
        "Bigger = better coverage."
    ),
    "ELITE PICKS": (
        "**Stocks passing ALL slider filters on both sides.**\n\n"
        "Default thresholds (Composite ≥ 80, EPS ≥ 80, A/D ≥ B+, U/D Vol ≥ 1.5, "
        "RSI ≤ 80) target the IBD-elite tier. Loosen any slider to widen the net; "
        "tighten to narrow it.\n\n"
        "**Interpretation:**\n"
        "- 15-30 = healthy elite leadership\n"
        "- 5-15 = narrow leadership, be selective\n"
        "- 0-5 = market not offering much; don't force trades"
    ),
}


# ---------------------------------------------------------------------------
# Slider tooltip docs (rich hover cards replacing Streamlit's blue ? icon)
# ---------------------------------------------------------------------------

_TS_SLIDER_DOCS = {
    "MIN COMPOSITE RATING": (
        "**Hide stocks with Composite Rating below this value.**\n\n"
        "Composite blends EPS Rating (40%), RS (35%), Acc/Dis (15%), and Group RS (10%) "
        "into a single 1-99 master quality score.\n\n"
        "**Suggested thresholds:**\n"
        "- 0 = no filter\n"
        "- 70 = decent quality (top 30%)\n"
        "- 80 = IBD 'investment grade' — default (top 20%)\n"
        "- 90 = elite tier (top 10%)\n"
        "- 95 = ultra-elite (top 5%)"
    ),
    "MIN EPS RATING": (
        "**Hide stocks with an EPS Rating below this value.**\n\n"
        "1-99 percentile of forward EPS growth across the universe.\n\n"
        "**Suggested thresholds:**\n"
        "- 0 = no filter\n"
        "- 70 = above-average growth\n"
        "- 80 = IBD 'strong earnings' — default\n"
        "- 90 = elite growth"
    ),
    "MIN A/D RATING": (
        "**Hide stocks below this Accumulation/Distribution letter grade.**\n\n"
        "13-week price-volume divergence. A+/A = institutions buying hard; "
        "E/D = distribution.\n\n"
        "**Suggested settings:**\n"
        "- E = no filter\n"
        "- C+ = exclude the worst distribution\n"
        "- B+ = IBD elite threshold — default\n"
        "- A = only strong accumulation\n"
        "- A+ = strongest accumulation only"
    ),
    "MIN U/D VOL RATIO": (
        "**Hide stocks below this 50-day up-volume / down-volume ratio.**\n\n"
        "Sum of volume on up-days divided by sum of volume on down-days, last 50 sessions.\n\n"
        "**Suggested thresholds:**\n"
        "- 0.0 = no filter\n"
        "- 1.0 = balanced\n"
        "- 1.5 = strong accumulation — default\n"
        "- 2.0 = very heavy buying\n"
        "- 3.0+ = climactic"
    ),
    "MAX RSI": (
        "**Hide stocks with 14-day RSI above this value.**\n\n"
        "RSI above 70-80 is the standard 'overbought' zone — starting a new position "
        "there often means buying near a short-term peak. This filter keeps Top Stocks "
        "to names with room to run before they get extended.\n\n"
        "**Suggested thresholds:**\n"
        "- 100 = no filter\n"
        "- 85 = filter out only the most extended names\n"
        "- 80 = standard overbought line — default\n"
        "- 75 = conservative (excludes more)\n"
        "- 70 = very conservative (Wilder's classic overbought line)"
    ),
    "MAX INDUSTRY RANK": (
        "**Hide stocks whose industry group ranks worse than this number.**\n\n"
        "Industry_Rank ranges 1 (strongest) to ~150 (weakest). O'Neil's research "
        "found ~49% of a stock's price move is attributable to its industry group's "
        "strength. Filtering to top-N industries is the cleanest single-knob "
        "quality bump available.\n\n"
        "**Suggested thresholds:**\n"
        "- 150 = no filter (default)\n"
        "- 30 = top-30 industries (loose quality cut)\n"
        "- 15 = top-15 industries (IBD typical leadership zone)\n"
        "- 10 = top-10 industries (concentrated leadership)\n"
        "- 5  = top-5 industries (extreme concentration)"
    ),
}


# ---------------------------------------------------------------------------
# Lightweight markdown → HTML for the hover tooltips
# ---------------------------------------------------------------------------

def _md_to_tooltip_html(md: str) -> str:
    """Convert the small subset of markdown used in tooltips to HTML.
    Handles **bold**, bullet lists, and paragraph breaks."""
    if not md:
        return ""
    html_parts = []
    in_list = False
    for raw in md.strip().split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<div style='height:0.4rem'></div>")
            continue
        # Bullet lines
        if line.lstrip().startswith("- "):
            if not in_list:
                html_parts.append("<ul style='margin:0.1rem 0 0.1rem 1.1rem;padding:0'>")
                in_list = True
            item = line.lstrip()[2:]
            item = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", item)
            html_parts.append(f"<li style='margin:0.1rem 0'>{item}</li>")
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            line_html = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
            html_parts.append(f"<div>{line_html}</div>")
    if in_list:
        html_parts.append("</ul>")
    return "".join(html_parts)


def _slider_hover_label(name: str) -> str:
    """HTML for a hoverable slider label with rich tooltip card."""
    doc_html = _md_to_tooltip_html(_TS_SLIDER_DOCS.get(name, ""))
    return (
        f"<div class='ts-sl-label'>"
        f"<div class='ts-sl-name'>{name}</div>"
        f"<div class='ts-sl-tooltip'>"
        f"<div class='ts-tooltip-title'>&#9656; {name}</div>"
        f"{doc_html}"
        f"</div></div>"
    )


# ---------------------------------------------------------------------------
# CSS — hover cards, sliders, and table tweaks (scoped to .ts- classes)
# ---------------------------------------------------------------------------

_TS_CSS = """
<style>
.ts-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.8rem 0;
}
.ts-card {
    position: relative;
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: center;
    cursor: help;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.ts-card:hover {
    border-color: #00D4FF;
    box-shadow: 0 0 0 1px rgba(0,212,255,0.25);
}
.ts-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.ts-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
.ts-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    margin-top: 0.25rem;
    font-weight: 600;
}
.ts-delta.up   { color: #00FF88; }
.ts-delta.down { color: #FF4D6D; }
.ts-tooltip {
    position: absolute;
    top: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    background: #0F1620;
    border: 1px solid #00D4FF;
    border-radius: 6px;
    padding: 0.85rem 1rem;
    width: 320px;
    max-width: 90vw;
    z-index: 9999;
    color: #E5E7EB;
    font-family: Inter, sans-serif;
    font-size: 0.85rem;
    line-height: 1.45;
    text-align: left;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transition: opacity 0.15s ease, visibility 0.15s ease;
    box-shadow: 0 8px 24px rgba(0,0,0,0.6);
}
.ts-card:hover .ts-tooltip { opacity: 1; visibility: visible; }
.ts-tooltip-title {
    font-family: 'JetBrains Mono', monospace;
    color: #00D4FF;
    font-size: 0.85rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
    letter-spacing: 0.04em;
}
.ts-grid .ts-card:nth-child(3) .ts-tooltip { left: auto; right: 0; transform: none; }
.ts-grid .ts-card:nth-child(1) .ts-tooltip { left: 0; transform: none; }

.ts-sl-label {
    position: relative;
    background: transparent;
    padding: 0;
    margin-bottom: 0.15rem;
    cursor: help;
    display: inline-block;
}
.ts-sl-name {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: #FFFFFF;
    font-weight: 700;
    letter-spacing: 0.06em;
}
.ts-sl-tooltip {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    background: #0F1620;
    border: 1px solid #00D4FF;
    border-radius: 6px;
    padding: 0.85rem 1rem;
    width: 320px;
    max-width: 90vw;
    z-index: 9999;
    color: #E5E7EB;
    font-family: Inter, sans-serif;
    font-size: 0.85rem;
    line-height: 1.45;
    text-align: left;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transition: opacity 0.15s ease, visibility 0.15s ease;
    box-shadow: 0 8px 24px rgba(0,0,0,0.6);
}
.ts-sl-label:hover .ts-sl-tooltip { opacity: 1; visibility: visible; }
</style>
"""


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def _render_top_stocks_tiles(long_count: int, quality_count: int,
                              intersection_count: int, elite_count: int):
    """Render three hover-card tiles (Momentum, Quality, Elite).

    INTERSECTION tile was removed per user request — it was essentially
    redundant (in practice equals Momentum count because Quality covers
    the full universe). The remaining three tiles span the full width.
    """
    # Elite delta — qualitative bucket vs the intersection pool
    if intersection_count > 0:
        ratio = elite_count / intersection_count
        if ratio >= 0.20:
            elite_msg, elite_cls = "↑ Broad strength", "up"
        elif ratio >= 0.05:
            elite_msg, elite_cls = "↑ Selective", "up"
        else:
            elite_msg, elite_cls = "↓ Narrow", "down"
    else:
        elite_msg, elite_cls = None, "neutral"

    tiles = [
        ("MOMENTUM CANDIDATES", f"{long_count:,}",    None, "neutral"),
        ("QUALITY UNIVERSE",    f"{quality_count:,}", None, "neutral"),
        ("ELITE PICKS",         f"{elite_count:,}",   elite_msg, elite_cls),
    ]

    st.markdown(_TS_CSS, unsafe_allow_html=True)
    cards_html = ["<div class='ts-grid'>"]
    for label, value, delta_msg, delta_cls in tiles:
        doc_html = _md_to_tooltip_html(_TS_METRIC_DOCS.get(label, ""))
        delta_html = (
            f"<div class='ts-delta {delta_cls}'>{delta_msg}</div>" if delta_msg else ""
        )
        cards_html.append(
            f"<div class='ts-card'>"
            f"<div class='ts-label'>{label}</div>"
            f"<div class='ts-value'>{value}</div>"
            f"{delta_html}"
            f"<div class='ts-tooltip'>"
            f"<div class='ts-tooltip-title'>&#9656; {label}</div>"
            f"{doc_html}"
            f"</div></div>"
        )
    cards_html.append("</div>")
    st.markdown("".join(cards_html), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# A/D ordering for the select_slider
# ---------------------------------------------------------------------------

# Worst → best, same as Quality view, so users see a familiar scale.
_AD_LADDER = ["E", "D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+"]


# ---------------------------------------------------------------------------
# TOC dialog
# ---------------------------------------------------------------------------

_TS_SIGNAL_DOCS = {
    "BUY ZONE": (
        "Minervini Triple Barrel composite — the ANTICIPATORY entry signal "
        "(fires BEFORE the obvious breakout). All four required: "
        "(1) trend template intact (free for every Momentum candidate), "
        "(2) 20-day Pressure ≥1.3 (up-volume / down-volume — institutional "
        "accumulation), "
        "(3) price within -3% to +10% of 50MA (near support, NOT chasing), "
        "(4) VCP pattern present (any grade — structure is forming). "
        "The point: take a defined-risk entry near 50MA with a tight stop "
        "underneath, BEFORE the breakout fires when stops would be wide and "
        "you'd already be extended. This is the highest-leverage signal in "
        "the app for finding low-risk entries early."
    ),
    "BO TODAY": (
        "Breakout above the buy pivot TODAY on volume ≥1.5× the 50-day average. "
        "Actionable signal if you're stalking the name — entry near the pivot, "
        "stop below the last contraction low."
    ),
    "CHEAT": (
        "Minervini \"Volatility Contraction Cheat\" entry — earlier than a full "
        "breakout. Today's close cleared the high of the MOST RECENT contraction "
        "(not yet the full base pivot) on confirmation volume. Cuts down wait "
        "time on the cleanest VCPs, but only valid when the VCP is tight. "
        "Risk: stop goes below the contraction low; pivot is your second target."
    ),
    "POWER PLAY": (
        "Minervini high-tight-flag setup — TIGHTENED 2026-06-30. Requires ALL of: "
        "(1) +100% in 8 weeks, (2) ≥10 trading bars since peak, (3) ≤15% off peak, "
        "(4) 5-day daily range ≤ prior 20-day range (volatility contracting), "
        "(5) 5-day avg volume < 80% of 50-day avg (volume drying up), "
        "(6) close above 10-EMA (holding the flag), (7) no distribution day in "
        "last 5 sessions. Rare and powerful when it fires cleanly."
    ),
    "GAP UP": (
        "Today's open gapped ≥2% above prior close, held the gap (close ≥ open), "
        "and traded on volume >1.5× the 50-day average. Usually a catalyst day "
        "(earnings beat, news, upgrade). Tradable when the gap HOLDS — fillers "
        "are head-fakes. Pair with strong fundamentals before sizing in."
    ),
    "RS✓": (
        "Relative Strength line at a new 50-day high. Tells you the stock is "
        "leading the market even within its base — classic institutional-"
        "accumulation signal that often precedes the obvious breakout."
    ),
    "VCP-A": (
        "Grade-A Volatility Contraction Pattern: 3+ progressively tighter "
        "contractions, each ≤75% of the prior, current contraction <6% deep, "
        "volume drying up on each successive low. The cleanest Minervini "
        "setup — usually best risk/reward."
    ),
    "VCP-B": (
        "Grade-B VCP: 2+ contractions, final contraction <10% deep. Solid "
        "multi-contraction structure but less pristine than A. Still tradable, "
        "often the workhorse signal when A-grade setups are scarce."
    ),
}

_TS_WARNING_DOCS = {
    "EXT": (
        "Extended: price >20% above the 50-day moving average. Late-stage "
        "entries here carry significant giveback risk. The stop typically "
        "has to sit too far below the entry, blowing the risk budget."
    ),
    "ER Nd": (
        "Earnings in N days (0-7 only — beyond 7 days isn't flagged). "
        "Binary catalyst risk: stocks can gap 10-20% on the print. Most "
        "Minervini-style traders avoid initiating new swings within ~7 days "
        "of earnings, holding existing winners through if they have profit cushion."
    ),
    "WIDE": (
        "Risk % from buy pivot to stop is >7%. For a $10k account at 1.25% "
        "risk per trade ($125), this means very small position size. Either "
        "wait for a tighter setup or accept the smaller size — don't loosen "
        "the stop to take a bigger position."
    ),
    "SQUAT": (
        "Bearish reversal candle today — close in the lower half of the "
        "daily range with the high above recent highs. Classic distribution "
        "pattern: stock pushed higher intraday, sellers stepped in, close "
        "near the lows. Avoid initiating; existing positions watch closely."
    ),
}


_TS_COLUMN_DOCS = {
    "Ticker": "Stock symbol. Click a row to open the full deep dive (Chart, Execution, Fundamentals, Social, IBD).",
    "Sector": "Top-level GICS sector (e.g. Tech, Industrials, Healthcare).",
    "Industry": "GICS industry inside the sector (e.g. Semiconductors, Steel).",
    "Composite_Rating": "Master IBD-style quality score 1-99. Blends EPS (40%) + RS (35%) + A/D (15%) + Group RS (10%). Default filter: ≥ 80.",
    "EPS_Rating": "1-99 percentile of forward EPS growth across the universe. Default filter: ≥ 80.",
    "Acc_Dis_Rating": "A+ to E letter grade for institutional accumulation vs distribution over the last 13 weeks. Default filter: ≥ B+.",
    "UD_Vol_Ratio": "50-day up-volume divided by down-volume. >1.0 = net accumulation. Default filter: ≥ 1.5.",
    "RS_Rating": "1-99 percentile of 6-month price performance across the universe.",
    "RSI": "14-day Relative Strength Index from the Momentum scan. Default filter: ≤ 80 (avoid starting new positions in overbought names).",
    "Price": "Last closing price from the Momentum scan.",
    "Risk_Pct": "Percentage risk from buy pivot to stop. Lower is better for position sizing. Open the deep dive (click the row) to see the actual Buy and Stop prices in the Execution tab.",
    "Signals": "Bullish setup tags from the Momentum scan. See the SIGNALS section below for definitions of each tag (BO TODAY, CHEAT, POWER PLAY, GAP UP, RS✓, VCP-A, VCP-B).",
    "Warnings": "Caution tags from the Momentum scan. See the WARNINGS section below for definitions of each tag (EXT, ER Nd, WIDE, SQUAT).",
    "Fwd_EPS_Growth": "Forward EPS growth percentage from the Quality scan's estimates.",
}


@st.dialog("Table of Contents", width="large")
def _ts_toc_dialog():
    st.markdown(
        "<div style='font-family:Inter;font-size:0.95rem;color:#E5E7EB;line-height:1.55'>"
        "<b>Top Stocks = intersection of SEPA Momentum scan ∩ IBD Quality scan.</b><br>"
        "Stocks here pass <b>both</b> the technical setup criteria <i>and</i> elite "
        "fundamental quality. This is the highest-conviction list in the app."
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    st.markdown("<h4 style='margin-bottom:0.5rem'>📊 Summary tiles</h4>", unsafe_allow_html=True)
    for label, doc in _TS_METRIC_DOCS.items():
        st.markdown(f"**{label}**", unsafe_allow_html=True)
        st.markdown(doc)
        st.markdown("")

    st.markdown("---")
    st.markdown("<h4 style='margin-bottom:0.5rem'>🎚 Filter sliders</h4>", unsafe_allow_html=True)
    for label, doc in _TS_SLIDER_DOCS.items():
        st.markdown(f"**{label}**", unsafe_allow_html=True)
        st.markdown(doc)
        st.markdown("")

    st.markdown("---")
    st.markdown("<h4 style='margin-bottom:0.5rem'>📋 Table columns</h4>", unsafe_allow_html=True)
    for col, doc in _TS_COLUMN_DOCS.items():
        st.markdown(f"- **{col}** — {doc}")

    st.markdown("---")
    st.markdown("<h4 style='margin-bottom:0.5rem'>🟢 SIGNALS (bullish setup tags)</h4>", unsafe_allow_html=True)
    for tag, doc in _TS_SIGNAL_DOCS.items():
        st.markdown(f"- **{tag}** — {doc}")

    st.markdown("---")
    st.markdown("<h4 style='margin-bottom:0.5rem'>🔴 WARNINGS (caution tags)</h4>", unsafe_allow_html=True)
    for tag, doc in _TS_WARNING_DOCS.items():
        st.markdown(f"- **{tag}** — {doc}")


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def render_top_stocks_page():
    """Default home page — the intersection of Long + Quality scans."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    yellow = PALETTE["signal_yellow"]
    red = PALETTE["down_red"]

    # Breadcrumb-style header (mirrors quality_view + others)
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{muted};"
        f"padding-top:0.5rem'>TOP STOCKS  /  Momentum ∩ Quality intersection</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<h2 style='margin-top:0.6rem'>⭐ Top Stocks</h2>"
        f"<div style='color:{muted};font-size:0.9rem;line-height:1.5;margin-bottom:1rem;max-width:900px'>"
        f"The intersection of the SEPA Momentum scan and the IBD Quality scan — only stocks "
        f"passing <b>both</b> technical setup criteria and elite fundamental quality. "
        f"Default filters target IBD-elite thresholds (Composite ≥ 80, EPS ≥ 80, "
        f"A/D ≥ B+, U/D Vol ≥ 1.5, RSI ≤ 80). Loosen the sliders to widen the net."
        f"</div>",
        unsafe_allow_html=True,
    )

    # ---- Pull the user's slider state (with default fallbacks) ----
    # Industry-rank slider is read here too so we can apply its filter BEFORE
    # rendering the ELITE PICKS tile (fixes 2026-06-17 bug where the tile
    # showed the pre-industry-filter count and disagreed with the table below).
    min_comp     = st.session_state.get("ts_min_composite", DEFAULT_MIN_COMPOSITE)
    min_eps      = st.session_state.get("ts_min_eps", DEFAULT_MIN_EPS)
    min_ad       = st.session_state.get("ts_min_ad", DEFAULT_MIN_AD)
    min_ud       = st.session_state.get("ts_min_ud_vol", DEFAULT_MIN_UD_VOL)
    max_rsi      = st.session_state.get("ts_max_rsi", DEFAULT_MAX_RSI)
    max_ind_rank = int(st.session_state.get("ts_max_ind_rank", 150))

    # ---- Compute ----
    result = compute_top_stocks(
        min_composite=min_comp,
        min_eps=min_eps,
        min_ad=min_ad,
        min_ud_vol=min_ud,
        max_rsi=max_rsi,
    )

    # ---- Apply the industry-rank slider BEFORE tile rendering ----
    # compute_top_stocks() doesn't know about Industry_Rank, so the industry
    # filter has to be applied here. The post-filter count is what feeds BOTH
    # the ELITE PICKS tile AND the table below — keeping them in sync.
    candidates_filtered = result.candidates or []
    if max_ind_rank < 150:
        candidates_filtered = [
            c for c in candidates_filtered
            if c.get("Industry_Rank") is None
            or (isinstance(c.get("Industry_Rank"), (int, float))
                and c["Industry_Rank"] <= max_ind_rank)
        ]
    elite_count_after_all_filters = len(candidates_filtered)

    # ---- Freshness warning (your answer #3: warning + empty until both fresh) ----
    if not result.both_fresh:
        missing_msgs = []
        if result.long_timestamp is None:
            missing_msgs.append("**Momentum scan has never been run.**")
        elif not result.long_fresh:
            missing_msgs.append(
                f"**Momentum scan is older than {FRESHNESS_HOURS}h** "
                f"(timestamp: {result.long_timestamp})."
            )
        if result.quality_timestamp is None:
            missing_msgs.append("**Quality scan has never been run.**")
        elif not result.quality_fresh:
            missing_msgs.append(
                f"**Quality scan is older than {FRESHNESS_HOURS}h** "
                f"(timestamp: {result.quality_timestamp})."
            )

        warning_body = (
            f"<div style='font-family:Inter;font-size:0.95rem;color:#E5E7EB;line-height:1.6'>"
            f"<b style='color:{yellow}'>⚠ Top Stocks needs fresh scans on both sides.</b><br><br>"
            + "<br>".join(missing_msgs)
            + f"<br><br>Run both scans (Momentum + Quality) within the last {FRESHNESS_HOURS} hours to "
            f"populate this page. Use the buttons below to jump to each scanner."
            f"</div>"
        )
        st.markdown(panel("STALE OR MISSING SCANS", warning_body), unsafe_allow_html=True)

        # Shortcut buttons to the two scanners
        st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
        c1, c2, _ = st.columns([1, 1, 2])
        with c1:
            if st.button("📈 Go to Momentum scanner", type="primary",
                         use_container_width=True, key="ts_go_long"):
                st.session_state.mode = "longs"
                st.session_state.view = "dashboard"
                st.rerun()
        with c2:
            if st.button("💎 Go to Quality scanner", type="primary",
                         use_container_width=True, key="ts_go_quality"):
                st.session_state.mode = "quality"
                st.session_state.view = "quality"
                st.rerun()

        # Still show tiles so user sees the counts that ARE available
        st.markdown("---")
        _render_top_stocks_tiles(
            result.long_count, result.quality_count,
            result.intersection_count, result.elite_count,
        )
        return

    # ---- Tiles ----
    # ELITE PICKS uses the post-industry-filter count so the tile matches the
    # table row count exactly. The other tiles are scan-level and unaffected
    # by per-page filters.
    _render_top_stocks_tiles(
        result.long_count, result.quality_count,
        result.intersection_count, elite_count_after_all_filters,
    )

    # ---- Filter sliders ----
    st.markdown("<div style='height:0.4rem'></div>", unsafe_allow_html=True)
    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1, 1, 1, 1, 1, 1])

    with sc1:
        st.markdown(_slider_hover_label("MIN COMPOSITE RATING"), unsafe_allow_html=True)
        st.slider(
            "MIN COMPOSITE RATING", 0, 99, value=int(min_comp), step=1,
            key="ts_min_composite", label_visibility="collapsed",
        )
    with sc2:
        st.markdown(_slider_hover_label("MIN EPS RATING"), unsafe_allow_html=True)
        st.slider(
            "MIN EPS RATING", 0, 99, value=int(min_eps), step=1,
            key="ts_min_eps", label_visibility="collapsed",
        )
    with sc3:
        st.markdown(_slider_hover_label("MIN A/D RATING"), unsafe_allow_html=True)
        st.select_slider(
            "MIN A/D RATING", options=_AD_LADDER,
            value=min_ad if min_ad in _AD_LADDER else DEFAULT_MIN_AD,
            key="ts_min_ad", label_visibility="collapsed",
        )
    with sc4:
        st.markdown(_slider_hover_label("MIN U/D VOL RATIO"), unsafe_allow_html=True)
        st.slider(
            "MIN U/D VOL RATIO", 0.0, 5.0, value=float(min_ud), step=0.1,
            key="ts_min_ud_vol", label_visibility="collapsed",
        )
    with sc5:
        st.markdown(_slider_hover_label("MAX RSI"), unsafe_allow_html=True)
        st.slider(
            "MAX RSI", 50, 100, value=int(max_rsi), step=1,
            key="ts_max_rsi", label_visibility="collapsed",
        )
    with sc6:
        st.markdown(_slider_hover_label("MAX INDUSTRY RANK"), unsafe_allow_html=True)
        st.slider(
            "MAX INDUSTRY RANK", 1, 150,
            value=int(st.session_state.get("ts_max_ind_rank", 150)), step=1,
            key="ts_max_ind_rank", label_visibility="collapsed",
        )

    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)

    # ---- The table ----
    # Use the already-industry-filtered list from above so the table count
    # matches the ELITE PICKS tile exactly.
    candidates = candidates_filtered
    if not candidates:
        st.info(
            f"No stocks pass the current filters. "
            f"{result.intersection_count} stocks in the intersection — "
            "loosen the sliders to see more."
        )
        return

    # Build the dataframe — drop the carried-along _long/_quality private cols.
    # Also drop Buy_Trigger and Stop per user request (2026-06-02): the row
    # itself opens the deep dive, where the user can see those values inside
    # the Execution tab. Keeping them out of the table keeps it cleaner.
    df = pd.DataFrame(candidates).drop(
        columns=["_long", "_quality", "Buy_Trigger", "Stop"],
        errors="ignore",
    )

    # Render Signals + Warnings as comma-joined strings for the table
    def _join_tags(v):
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return v or ""
    df["Signals"] = df["Signals"].apply(_join_tags)
    df["Warnings"] = df["Warnings"].apply(_join_tags)

    # Friendlier display column names
    display = df.rename(columns={
        "Composite_Rating": "Composite",
        "EPS_Rating":       "EPS",
        "Acc_Dis_Rating":   "A/D",
        "UD_Vol_Ratio":     "U/D Vol",
        "RS_Rating":        "RS",
        "Risk_Pct":         "Risk %",
        "Fwd_EPS_Growth":   "Fwd EPS Growth %",
    })

    # Column formatting notes:
    #   - alignment="center" centers CELL content (Streamlit 1.57+).
    #   - HEADER alignment cannot be set via Streamlit — the data grid is
    #     rendered on a <canvas>, so CSS can't reach the header text. The
    #     workaround attempt: pad short header labels with whitespace so they
    #     appear closer to centered. Wide headers ("Composite", "Warnings")
    #     already fill their column and don't need padding.
    #   - format="%,.1f%%" → "3,233.3%" — comma thousand-sep + 1 decimal + literal %.
    #   - Price uses "$%,.2f" so prices over $1,000 get a thousands separator.
    #   - Fwd EPS Growth % width=170 hand-sized to fit the full header label.
    column_config = {
        "Ticker":           st.column_config.TextColumn("Ticker", pinned=True, width="small"),
        "Sector":           st.column_config.TextColumn("Sector", width="small"),
        "Industry":         st.column_config.TextColumn("Industry", width="medium"),
        "Composite":        st.column_config.NumberColumn("Composite",            format="%d",     width="small",  alignment="center"),
        "EPS":              st.column_config.NumberColumn("    EPS",              format="%d",     width="small",  alignment="center"),
        "A/D":              st.column_config.TextColumn  ("    A/D",                               width="small",  alignment="center"),
        "U/D Vol":          st.column_config.NumberColumn("  U/D Vol",            format="%.2f",   width="small",  alignment="center"),
        "RS":               st.column_config.NumberColumn("     RS",              format="%d",     width="small",  alignment="center"),
        "RSI":              st.column_config.NumberColumn("    RSI",              format="%.1f",   width="small",  alignment="center"),
        "Price":            st.column_config.NumberColumn("   Price",             format="$%,.2f", width="small",  alignment="center"),
        "Risk %":           st.column_config.NumberColumn("  Risk %",             format="%.2f%%", width="small",  alignment="center"),
        "Signals":          st.column_config.TextColumn  ("  Signals",                             width="small"),
        "Warnings":         st.column_config.TextColumn  ("Warnings",                              width="small"),
        "Fwd EPS Growth %": st.column_config.NumberColumn(" Fwd EPS Growth %",
                                                          format="%,.1f%%",                       width=170,      alignment="center"),
    }

    # Cache filtered df for the sidebar exports
    st.session_state.ts_filtered_df = display

    selection = st.dataframe(
        display,
        use_container_width=True,
        height=min(720, 90 + len(display) * 36),
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
    )

    # Row click → deep dive, with origin=top_stocks so Return-to-Home brings us back
    if selection and "selection" in selection and selection["selection"].get("rows"):
        idx = selection["selection"]["rows"][0]
        ticker = display.iloc[idx]["Ticker"]
        st.session_state.deep_dive_ticker = ticker
        st.session_state.deep_dive_origin = "top_stocks"
        st.session_state.view = "deepdive"
        st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_top_stocks_sidebar():
    """Sidebar for the Top Stocks home page.

    Shows:
      - Run-Scan shortcuts (jump to Long / Quality scanners)
      - Excel + Tickers downloads of the current filtered table
      - Table of Contents button
      - Last-scan timestamps for both sides so the user knows freshness
    """
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    yellow = PALETTE["signal_yellow"]
    up = PALETTE["up_green"]

    # Title strip
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem;line-height:1.2'>⭐ LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem'>TOP STOCKS — HOME</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.0rem'>"
        f"Momentum ∩ Quality — only the very best.</div>",
        unsafe_allow_html=True,
    )

    # ---- Run scan shortcuts (route to each scanner) ----
    st.markdown(f"<div class='panel-title'>RUN SCANS</div>", unsafe_allow_html=True)
    if st.button("📈 Run Momentum Scan", type="primary", use_container_width=True,
                 key="ts_sidebar_run_long",
                 help="Switch to the Momentum scanner page where Run Scan lives."):
        st.session_state.mode = "longs"
        st.session_state.view = "dashboard"
        st.rerun()
    if st.button("💎 Run Quality Scan", type="primary", use_container_width=True,
                 key="ts_sidebar_run_quality",
                 help="Switch to the Quality scanner page where Run Scan lives."):
        st.session_state.mode = "quality"
        st.session_state.view = "quality"
        st.rerun()

    st.markdown("---")

    # ---- Downloads (use the filtered df cached by the page) ----
    df = st.session_state.get("ts_filtered_df")
    has_results = df is not None and len(df) > 0

    if has_results:
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                df,
                sheet_name="Top Stocks",
                title=f"Legacy Brands - Top Stocks {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"legacy_brands_top_stocks_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")

        tickers_text = "\n".join(df["Ticker"].astype(str).tolist())
        stamp = datetime.utcnow().strftime("%d%b%Y")
        st.download_button(
            label="📥 Download Tickers (.txt)",
            data=tickers_text,
            file_name=f"legacy_brands_top_stocks_tickers_{stamp}.txt",
            mime="text/plain",
            use_container_width=True,
            type="primary",
        )
        st.caption(f"Exports include the {len(df)} elite picks currently visible.")
    else:
        st.caption("Elite picks list is empty — run both scans, then adjust filters.")

    if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                 key="ts_sidebar_toc",
                 help="Definitions for every column, slider, and summary tile"):
        _ts_toc_dialog()

    st.markdown("---")

    # ---- Last scan timestamps for both sides ----
    from scanner.cache import load_latest_scan
    from scanner.quality import load_latest_quality_scan
    long_scan = load_latest_scan() or {}
    q_scan = load_latest_quality_scan() or {}

    long_ts = long_scan.get("timestamp", "")
    q_ts = q_scan.get("timestamp", "")

    def _scan_age(ts):
        # Local helper so we don't depend on app.py's _scan_age_string (which
        # imports happen one direction only — UI -> app would be circular).
        from datetime import datetime as _dt, timezone as _tz
        if not ts:
            return None
        try:
            s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            hours = (_dt.now(_tz.utc) - dt).total_seconds() / 3600
            if hours < 1:
                return f"{int(hours * 60)}m old"
            if hours < 48:
                return f"{int(hours)}h old"
            return f"{int(hours / 24)}d old"
        except Exception:
            return None

    long_age = _scan_age(long_ts)
    q_age = _scan_age(q_ts)

    def _stamp_color(age_str):
        if not age_str: return muted
        if age_str.endswith("d old"): return yellow  # >48h
        return cyan

    long_html = (
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem'>"
        f"Momentum: <span style='color:{_stamp_color(long_age)}'>{long_age or 'never'}</span><br>"
        f"Quality:  <span style='color:{_stamp_color(q_age)}'>{q_age or 'never'}</span></div>"
    )
    st.markdown(panel("LAST SCANS", long_html), unsafe_allow_html=True)
