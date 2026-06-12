"""
Quality mode UI — IBD-style fundamental quality table.

Wholly separate from the Long-scan dashboard:
  - Own page renderer (render_quality_page)
  - Own sidebar (render_quality_sidebar)
  - Own scan trigger that calls scanner/quality.run_quality_scan
  - Reads/writes only data/cache/quality.json + data/scans/scan_quality_*.json

If this file is deleted, nothing else in the app breaks.
"""
from __future__ import annotations

import re
from datetime import datetime
import pandas as pd
import streamlit as st

from scanner.quality import (
    run_quality_scan,
    load_latest_quality_scan,
    QUALITY_SCANS_DIR,
)
from .style import PALETTE, panel
from .excel_export import format_dataframe_to_excel


# ---------------------------------------------------------------------------
# Hover-card metric tiles (same visual style as Long-scan breadth tiles)
# ---------------------------------------------------------------------------

_QUALITY_METRIC_DOCS = {
    "UNIVERSE": (
        "**Total tickers in the scan pool.**\n\n"
        "Russell 3000 by default — about 3,000-3,200 names. Not a quality metric per se; "
        "just the size of the universe we computed ratings against.\n\n"
        "If unusually low, Yahoo Finance may have rate-limited some downloads."
    ),
    "RATED": (
        "**Stocks that received at least a partial rating.**\n\n"
        "A stock makes it onto the table if we had cached price data AND at least some "
        "fundamentals to compute a Composite from. Usually 95-100% of Universe.\n\n"
        "**Interpretation:**\n"
        "- Close to Universe = healthy coverage\n"
        "- Significantly below = lots of stocks missing fundamentals on Yahoo Finance"
    ),
    "COMPOSITE ≥ 80": (
        "**Stocks scoring 80 or higher on the Composite Rating proxy.**\n\n"
        "IBD's threshold for 'investment-grade quality.' Composite blends EPS Rating, "
        "6-month RS, and Acc/Dis into a single 1-99 score.\n\n"
        "**Interpretation:**\n"
        "- 25%+ of universe = broadly strong market\n"
        "- 10-25% = healthy leadership\n"
        "- Under 10% = narrow leadership, defensive posture warranted"
    ),
    "EPS RATING ≥ 80": (
        "**Stocks in the top 20% of forward EPS growth across the universe.**\n\n"
        "Same threshold IBD uses for 'strong earnings.' EPS Rating is a percentile "
        "rank of forward EPS growth (Y1 vs trailing).\n\n"
        "**Interpretation:**\n"
        "- 15-30% of universe = healthy earnings backdrop\n"
        "- Under 15% = earnings cycle is weak; harder to find SEPA-quality setups"
    ),
    "ACC/DIS = A+/A": (
        "**Stocks under strong institutional accumulation.**\n\n"
        "A+/A means the stock has had heavy up-volume on recent rallies over the last "
        "13 weeks (high net accumulation days). Computed from price-volume divergence.\n\n"
        "**Interpretation:**\n"
        "- High counts = institutions are buying broadly (bullish)\n"
        "- Low counts + high D/E counts = distribution dominates (bearish)"
    ),
}


def _md_to_tooltip_html(md: str) -> str:
    """Tiny markdown -> HTML converter for hover tooltips.
    Mirrors the helper in app.py so we don't have a cross-module import."""
    out, in_list = [], False
    for raw in md.split("\n"):
        line = raw.strip()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul style='margin:0.3rem 0 0.3rem 1.05rem;padding:0'>")
                in_list = True
            item = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line[2:])
            out.append(f"<li style='margin:0.12rem 0'>{item}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            if line:
                line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
                out.append(f"<p style='margin:0.35rem 0'>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)




# Slider docs (rich tooltips that replace Streamlit's default blue-dot help icon)
# Sliders are now bidirectional (min + max) — 2026-06-11 update.
_QUALITY_SLIDER_DOCS = {
    "COMPOSITE RATING": (
        "**Filter stocks by Composite Rating range (min + max).**\n\n"
        "Composite is the 1-99 master quality score blending EPS Rating, 6-month RS, "
        "and Acc/Dis. Higher = better. Drag the left handle up to set a floor, "
        "the right handle down to set a ceiling.\n\n"
        "**Suggested thresholds:**\n"
        "- (0, 99) = no filter, show all stocks\n"
        "- (70, 99) = decent quality and above (top 30%)\n"
        "- (80, 99) = IBD 'investment grade' (top 20%)\n"
        "- (90, 99) = IBD 'elite' tier (top 10%)\n"
        "- (80, 90) = solid quality but NOT extreme (avoid late-stage climax names)"
    ),
    "EPS RATING": (
        "**Filter stocks by EPS Rating range (min + max).**\n\n"
        "EPS Rating is the 1-99 percentile of forward EPS growth across the "
        "universe. Higher = faster earnings growth relative to other stocks.\n\n"
        "**Suggested thresholds:**\n"
        "- (0, 99) = no filter\n"
        "- (70, 99) = above-average growth (top 30%)\n"
        "- (80, 99) = IBD's threshold for 'strong earnings'\n"
        "- (90, 99) = elite growth (top 10%)"
    ),
    "A/D": (
        "**Filter stocks by A/D rating range (worst min, best max).**\n\n"
        "A/D measures whether institutions are buying (accumulation) or selling "
        "(distribution) based on 13 weeks of price-volume patterns. Drag right "
        "handle in to exclude the very strongest accumulation; drag left handle "
        "in to exclude the weakest distribution.\n\n"
        "**Suggested settings:**\n"
        "- (E, A+) = no filter, show all stocks\n"
        "- (C+, A+) = exclude the worst distribution (E, D, C)\n"
        "- (B+, A+) = only mild-or-better accumulation (IBD elite threshold)\n"
        "- (A, A+) = only strong accumulation"
    ),
    "U/D VOL RATIO": (
        "**Filter stocks by 50-day up/down volume ratio range (min + max).**\n\n"
        "Exact IBD calculation: sum of volume on up-days divided by sum of volume "
        "on down-days, over the last 50 trading sessions. Higher = institutions "
        "buying harder than selling.\n\n"
        "**Suggested thresholds:**\n"
        "- (0.0, 5.0) = no filter\n"
        "- (1.0, 5.0) = balanced or better (net accumulation)\n"
        "- (1.5, 5.0) = strong accumulation (IBD elite threshold)\n"
        "- (1.5, 2.5) = strong but NOT climactic accumulation"
    ),
}


def _slider_hover_label(name: str) -> str:
    """Return HTML for a hoverable slider label with rich tooltip card.
    Renders just above the slider; the slider itself uses label_visibility='collapsed'."""
    doc_html = _md_to_tooltip_html(_QUALITY_SLIDER_DOCS.get(name, ""))
    return (
        f"<div class='q-sl-label'>"
        f"<div class='q-sl-name'>{name}</div>"
        f"<div class='q-sl-tooltip'>"
        f"<div class='q-tooltip-title'>&#9656; {name}</div>"
        f"{doc_html}"
        f"</div></div>"
    )


def _render_quality_tiles(df: pd.DataFrame, n_candidates: int, universe_size: int):
    """Render five hover-card tiles in a CSS grid (matches the Long-scan breadth cards)."""
    composite_above_80 = int((df["Composite_Rating"].fillna(0) >= 80).sum())
    eps_above_80       = int((df["EPS_Rating"].fillna(0) >= 80).sum())
    a_grades           = int(df["Acc_Dis_Rating"].isin(["A+", "A"]).sum())

    # Percentage of the FULL universe (not just rated) so denominator matches
    # what the user sees in the "UNIVERSE" tile.
    denom = max(1, universe_size)
    composite_pct = composite_above_80 / denom * 100
    eps_pct       = eps_above_80 / denom * 100
    ad_pct        = a_grades / denom * 100

    tiles = [
        ("UNIVERSE",        f"{universe_size:,}",            None, "neutral"),
        ("RATED",           f"{n_candidates:,}",             None, "neutral"),
        ("COMPOSITE ≥ 80",  f"{composite_above_80:,} ({composite_pct:.1f}%)",
            ("↑ Broad strength" if composite_above_80 >= int(0.10 * max(1, n_candidates)) else "↓ Narrow"),
            ("up" if composite_above_80 >= int(0.10 * max(1, n_candidates)) else "down")),
        ("EPS RATING ≥ 80", f"{eps_above_80:,} ({eps_pct:.1f}%)",
            ("↑ Strong" if eps_above_80 >= int(0.10 * max(1, n_candidates)) else "↓ Weak"),
            ("up" if eps_above_80 >= int(0.10 * max(1, n_candidates)) else "down")),
        ("ACC/DIS = A+/A",  f"{a_grades:,} ({ad_pct:.1f}%)",
            ("↑ Accumulation" if a_grades >= int(0.10 * max(1, n_candidates)) else "↓ Distribution"),
            ("up" if a_grades >= int(0.10 * max(1, n_candidates)) else "down")),
    ]

    css = """
<style>
.q-grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.8rem 0;
}
.q-card {
    position: relative;
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: center;
    cursor: help;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.q-card:hover {
    border-color: #00D4FF;
    box-shadow: 0 0 0 1px rgba(0,212,255,0.25);
}
.q-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.q-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
.q-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    margin-top: 0.25rem;
    font-weight: 600;
}
.q-delta.up   { color: #00FF88; }
.q-delta.down { color: #FF4D6D; }
.q-tooltip {
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
.q-card:hover .q-tooltip {
    opacity: 1;
    visibility: visible;
}
.q-tooltip-title {
    font-family: 'JetBrains Mono', monospace;
    color: #00D4FF;
    font-size: 0.85rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
    letter-spacing: 0.04em;
}
.q-grid .q-card:nth-child(4) .q-tooltip,
.q-grid .q-card:nth-child(5) .q-tooltip {
    left: auto; right: 0; transform: none;
}
.q-grid .q-card:nth-child(1) .q-tooltip,
.q-grid .q-card:nth-child(2) .q-tooltip {
    left: 0; transform: none;
}
.q-sl-label {
    position: relative;
    background: transparent;
    padding: 0;
    margin-bottom: 0.15rem;
    cursor: help;
    display: inline-block;
}
.q-sl-name {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: #FFFFFF;
    font-weight: 700;
    letter-spacing: 0.06em;
}
.q-sl-tooltip {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    background: #0F1620;
    border: 1px solid #00D4FF;
    border-radius: 6px;
    padding: 0.85rem 1rem;
    width: 340px;
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
.q-sl-label:hover .q-sl-tooltip {
    opacity: 1;
    visibility: visible;
}
.q-sl-pct {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: #00D4FF;
    margin-top: 0.1rem;
    letter-spacing: 0.04em;
}
</style>
"""
    cards = []
    for label, value, delta, delta_class in tiles:
        delta_html = (
            f"<div class='q-delta {delta_class}'>{delta}</div>"
            if delta is not None else "<div class='q-delta'>&nbsp;</div>"
        )
        doc_html = _md_to_tooltip_html(_QUALITY_METRIC_DOCS.get(label, ""))
        cards.append(
            f"<div class='q-card'>"
            f"<div class='q-label'>{label}</div>"
            f"<div class='q-value'>{value}</div>"
            f"{delta_html}"
            f"<div class='q-tooltip'>"
            f"<div class='q-tooltip-title'>&#9656; {label}</div>"
            f"{doc_html}"
            f"</div></div>"
        )
    st.markdown(css + f"<div class='q-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Table of Contents dialog (column + summary-tile explanations)
# ---------------------------------------------------------------------------

@st.dialog("Quality Scan — Table of Contents", width="large")
def _quality_toc_dialog():
    """Explains every column on the Quality table + what each summary tile means."""
    cyan = PALETTE["accent_cyan"]

    def hdr(text):
        st.markdown(
            f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;"
            f"border-radius:4px;font-weight:700;margin-top:0.8rem;margin-bottom:0.3rem'>"
            f"{text}</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.6rem;border-radius:4px'>"
        f"<b>What this scan does:</b> rates every stock in the Russell 3000 universe "
        f"on IBD-style fundamental quality. Most ratings are proxies (IBD's exact "
        f"algorithms are proprietary) but rank stocks in a similar order. Use the "
        f"sliders above the table to filter by Composite, EPS Rating, A/D, etc.</div>",
        unsafe_allow_html=True,
    )

    hdr("Summary tiles (top of page)")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Universe</b> — total tickers in the scan pool (~3,000-6,000). Not a "
        f"quality metric; just the size of the universe.<br><br>"
        f"<b>Rated</b> — stocks that got at least a partial rating (had price data "
        f"+ some fundamentals). Normally close to Universe size.<br><br>"
        f"<b>Composite ≥ 80</b> — stocks scoring ≥80 on the Composite proxy. IBD's "
        f"threshold for 'investment grade quality.' Healthy markets have 10-25% of "
        f"universe here. >25% = broadly strong, <10% = narrow leadership.<br><br>"
        f"<b>EPS Rating ≥ 80</b> — stocks in the top 20% of forward EPS growth. "
        f"Same threshold IBD uses for 'strong earnings.'<br><br>"
        f"<b>Acc/Dis = A+/A</b> — stocks under heavy institutional accumulation. "
        f"Heavy counts here = bullish institutional positioning."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("Table columns")
    rows = [
        ("Ticker",       "Stock symbol. Pinned to the left so it stays visible while you scroll right."),
        ("Sector",       "GICS sector (abbreviated). Quick categorization."),
        ("Industry",     "GICS industry (more granular than sector)."),
        ("Comp",         "Composite Rating proxy (1-99). Weighted blend of EPS Rating, 6-month RS, and Acc/Dis. IBD considers ≥80 'investment grade,' ≥90 'elite.' Default sort column."),
        ("EPS",          "EPS Rating proxy (1-99). Percentile of forward EPS growth across the universe. ≥80 = top 20% of growers."),
        ("RS 3m",        "3-month price return percentile (1-99). Shorter-term momentum."),
        ("RS 6m",        "6-month price return percentile (1-99). Used as the RS input for Composite."),
        ("A/D",          "Accumulation/Distribution rating: A+ (strongest accumulation), A, B+, B, C+, C, D, E (heaviest distribution). Computed from 13-week price-volume patterns."),
        ("U/D Vol",      "50-day up-volume / down-volume ratio. >1.5 = strong accumulation, 1.0-1.5 = neutral, <1.0 = distribution. Exact match to IBD's number."),
        ("PE",           "Trailing 12-month P/E ratio. Lower = cheaper, but context-dependent."),
        ("Fwd PE",       "Forward P/E using next-year EPS estimate. Often lower than trailing PE when EPS is growing."),
        ("Fwd EPS",      "Next-year EPS estimate (consensus analyst average from yfinance)."),
        ("Fwd Gr %",     "Forward EPS growth: (forward EPS - trailing EPS) / |trailing|. Can be very large for stocks coming off small/negative earnings."),
        ("ROE %",        "Return on Equity (trailing 12 months). >15% generally considered good, >25% excellent."),
        ("Margin %",     "Trailing profit margin. Industry-dependent; software 20%+, retail 2-5%, etc."),
        ("Inst %",       "Institutional ownership % (includes mutual funds, hedge funds, pension funds, ETFs). Higher = more institutional sponsorship. IBD looks for 40-60%+ as 'meaningful institutional interest.'"),
    ]
    body = "<table style='width:100%;color:black;background:#fff;border-collapse:collapse'>"
    body += "<tr style='background:#f5f5f5'><th style='padding:0.4rem;text-align:left;width:90px'>Column</th><th style='padding:0.4rem;text-align:left'>What it means</th></tr>"
    for col, desc in rows:
        body += f"<tr><td style='padding:0.4rem;border-top:1px solid #ddd;font-weight:700'>{col}</td><td style='padding:0.4rem;border-top:1px solid #ddd'>{desc}</td></tr>"
    body += "</table>"
    st.markdown(body, unsafe_allow_html=True)

    hdr("Sliders (above the table) — all are RANGE sliders with a min AND a max")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Composite Rating</b> — drag left handle to set floor, right to set ceiling. Default (0, 99) = show all.<br>"
        f"<b>EPS Rating</b> — range for EPS Rating. Default (0, 99).<br>"
        f"<b>A/D</b> — pick the worst-to-best A/D range you'll accept. Default (E, A+) = show all.<br>"
        f"<b>U/D Vol Ratio</b> — range for the 50-day up-vol/down-vol ratio. Default (0.0, 5.0) = show all.<br>"
        f"<b>Search</b> — partial-match filter on ticker, sector, or industry.<br><br>"
        f"<b>Tip:</b> for an IBD-style 'elite' screen, set Composite (90, 99), EPS (80, 99), A/D (B+, A+), U/D Vol (1.5, 5.0). "
        f"For a 'quality but not climactic' screen, cap the max instead — e.g. Composite (80, 90) avoids both weak quality AND extreme late-stage names."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("How accurate are these vs IBD's numbers?")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Exact matches:</b> U/D Vol Ratio, RS 3m, RS 6m, PE, Fwd PE, Forward EPS, "
        f"Fwd EPS Growth %, ROE %, Margin %.<br><br>"
        f"<b>Proxies (similar ranking, different exact numbers):</b> Composite Rating, "
        f"EPS Rating, Acc/Dis Rating. IBD's exact algorithms are proprietary; ours "
        f"use the same underlying concepts (percentile ranking, price-volume divergence, "
        f"weighted blending) and rank stocks similarly ~75-80% of the time.<br><br>"
        f"<b>Approximation:</b> Inst % is the broader 'all institutions' number rather "
        f"than IBD's narrower 'mutual fund' specifically (yfinance doesn't break out "
        f"mutual fund ownership separately)."
        f"</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_quality_sidebar():
    """Sidebar for QUALITY mode — header, start/refresh, exports, navigation."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem'>QUALITY SCAN</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
        f"margin-bottom:1rem'>IBD-style fundamental ratings across the Russell 3000 universe.</div>",
        unsafe_allow_html=True,
    )

    latest = load_latest_quality_scan()

    # ---- Scan trigger ----
    if st.button("▶ Run Scan", key="quality_start_btn", type="primary", use_container_width=True):
        _run_quality_scan_with_progress()
        st.rerun()

    # ---- Download buttons (only when we have data) ----
    if latest and (latest.get("candidates") or []):
        candidates = latest["candidates"]
        # Determine what to export — honor the user's slider filters if they
        # set any (the page stores filtered df in session state for this).
        filtered_df = st.session_state.get("quality_filtered_df")
        if filtered_df is None or len(filtered_df) == 0:
            filtered_df = pd.DataFrame(candidates)
        n_export = len(filtered_df)

        # ---- Excel export ----
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                filtered_df,
                sheet_name="Quality",
                title=f"Legacy Brands - Quality Ratings {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"quality_scan_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
                key="quality_excel_btn",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")

        # ---- Tickers .txt export ----
        try:
            tickers_text = "\n".join(filtered_df["Ticker"].astype(str).tolist())
            stamp = datetime.utcnow().strftime("%d%b%Y")
            st.download_button(
                label="📥 Download Tickers (.txt)",
                data=tickers_text,
                file_name=f"quality_tickers_{stamp}.txt",
                mime="text/plain",
                use_container_width=True,
                type="primary",
                key="quality_tickers_btn",
            )
        except Exception as e:
            st.caption(f"Tickers export error: {e}")

        # ---- Table of Contents ----
        if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                     help="Definitions for every column + slider + summary tile",
                     key="quality_toc_btn"):
            _quality_toc_dialog()

        st.caption(f"Exports include the {n_export:,} rows currently visible (after sliders).")
    else:
        st.caption("Run a Quality scan to enable exports + downloads.")

    st.markdown("---")

    # ---- Last Scan info ----
    if latest:
        n_cands = len(latest.get("candidates") or [])
        diag = latest.get("diagnostics") or {}
        ts = latest.get("timestamp") or ""
        ts_display = ts.split(".")[0].replace("T", " ").replace("Z", "") + " UTC"
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
            f"margin-top:0.25rem;line-height:1.5'>"
            f"<b style='color:{cyan}'>LAST SCAN</b><br>"
            f"{ts_display}<br>"
            f"{n_cands:,} stocks rated · "
            f"{diag.get('with_fundamentals',0)} with fundamentals · "
            f"{diag.get('with_price_data',0)} with prices"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
            f"margin-top:0.25rem'>No Quality scan yet — click the button above. "
            f"First scan takes ~15-25 minutes.</div>",
            unsafe_allow_html=True,
        )


def _run_quality_scan_with_progress():
    """Execute run_quality_scan with a Streamlit progress bar in the sidebar."""
    bar = st.progress(0.0, text="Quality scan starting…")

    stage_weights = {
        "quality_start":    0.01,
        "quality_universe": 0.05,
        "quality_fund":     0.70,
        "quality_calc":     0.20,
        "quality_done":     1.00,
    }
    last_pct = 0.0

    def _prog(stage, completed, total, msg):
        nonlocal last_pct
        stage_target = stage_weights.get(stage, 0.0)
        prev_weight = 0.0
        for k, w in stage_weights.items():
            if k == stage:
                break
            prev_weight = w
        stage_fraction = max(0.0, min(1.0, completed / max(1, total)))
        global_pct = prev_weight + (stage_target - prev_weight) * stage_fraction
        global_pct = max(last_pct, min(1.0, global_pct))
        last_pct = global_pct
        bar.progress(global_pct, text=f"[QUALITY · {stage.upper()}] {msg}")

    try:
        result = run_quality_scan(progress=_prog)
        bar.progress(1.0, text=f"Quality scan complete — {len(result.candidates)} stocks rated")
    except Exception as e:
        st.error(f"Quality scan failed: {e}")


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

# A/D ratings ordered worst → best (for the select_slider scale)
_AD_ORDER = ["E", "D", "C", "C+", "B", "B+", "A", "A+"]
_AD_RANK = {label: i for i, label in enumerate(_AD_ORDER)}


def render_quality_page():
    """Render the Quality dashboard — load latest scan, show sortable table with sliders."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<h2 style='color:{cyan};margin-top:0'>💎 Quality — IBD-Style Ratings</h2>"
        f"<div style='color:{muted};font-size:0.85rem;margin-bottom:1rem'>"
        f"Universe-wide fundamental quality scoring. Composite Rating blends EPS Rating, "
        f"6-month RS, and Accumulation/Distribution. Use the sliders to filter; click "
        f"any column header to sort.</div>",
        unsafe_allow_html=True,
    )

    latest = load_latest_quality_scan()
    if latest is None:
        st.info(
            "No Quality scan has been run yet. Use the **▶ Run Scan** "
            "button in the sidebar to populate this page. First scan takes ~15-25 "
            "minutes (full universe fundamentals + price calcs)."
        )
        return

    candidates = latest.get("candidates") or []
    if not candidates:
        st.warning(
            "Latest Quality scan returned 0 stocks. This usually means yfinance "
            "rate-limited or your local price cache is empty. Try refreshing — "
            "and make sure you've run at least one Momentum scan first to populate "
            "the price cache."
        )
        return

    df = pd.DataFrame(candidates)
    diag = latest.get("diagnostics") or {}

    # ---- Summary tiles (full universe stats — NOT affected by sliders) ----
    # Hover any tile to see its rich tooltip card (same visual pattern as the
    # Long-scan breadth tiles)
    _render_quality_tiles(df, len(candidates), int(diag.get("universe_size", 0)))

    # ---- Filter sliders (4 RANGE sliders side-by-side + search input) ----
    # 2026-06-11: converted from one-sided lower-bound to bidirectional range
    # sliders so you can cap quality scores too (e.g. 80-90 range avoids both
    # weak quality AND extreme/climactic late-stage names).
    sc1, sc2, sc3, sc4, sc5 = st.columns([1.2, 1.2, 1.2, 1.2, 2.0])
    with sc1:
        st.markdown(_slider_hover_label("COMPOSITE RATING"), unsafe_allow_html=True)
        composite_range = st.slider(
            " ",
            min_value=0, max_value=99, value=(0, 99), step=1,
            label_visibility="collapsed",
            key="quality_slider_composite",
        )
    with sc2:
        st.markdown(_slider_hover_label("EPS RATING"), unsafe_allow_html=True)
        eps_range = st.slider(
            " ",
            min_value=0, max_value=99, value=(0, 99), step=1,
            label_visibility="collapsed",
            key="quality_slider_eps",
        )
    with sc3:
        st.markdown(_slider_hover_label("A/D"), unsafe_allow_html=True)
        ad_range = st.select_slider(
            " ",
            options=_AD_ORDER,
            value=("E", "A+"),
            label_visibility="collapsed",
            key="quality_slider_ad",
        )
    with sc4:
        st.markdown(_slider_hover_label("U/D VOL RATIO"), unsafe_allow_html=True)
        ud_vol_range = st.slider(
            " ",
            min_value=0.0, max_value=5.0, value=(0.0, 5.0), step=0.1,
            label_visibility="collapsed",
            key="quality_slider_ud_vol",
        )
    with sc5:
        # Match the slider's hover-label height so the input lines up vertically
        st.markdown(_slider_hover_label("SEARCH"), unsafe_allow_html=True)
        search_q = st.text_input(
            " ",
            placeholder="e.g. NVDA or tech",
            label_visibility="collapsed",
            key="quality_search",
            help="Filter rows by ticker, sector, or industry (case-insensitive).",
        )

    # ---- Apply filters ----
    filtered = df.copy()
    # Composite range
    cmin, cmax = composite_range
    if cmin > 0 or cmax < 99:
        comp = filtered["Composite_Rating"].fillna(0)
        filtered = filtered[(comp >= cmin) & (comp <= cmax)]
    # EPS range
    emin, emax = eps_range
    if emin > 0 or emax < 99:
        eps = filtered["EPS_Rating"].fillna(0)
        filtered = filtered[(eps >= emin) & (eps <= emax)]
    # A/D range (ordinal — convert each row's letter grade to its rank, then
    # check it falls inside [min_rank, max_rank]; missing values stay as -1
    # and are excluded if either bound is set above E).
    ad_lo, ad_hi = ad_range
    if ad_lo != "E" or ad_hi != "A+":
        lo_rank, hi_rank = _AD_RANK[ad_lo], _AD_RANK[ad_hi]
        rank_series = filtered["Acc_Dis_Rating"].map(
            lambda v: _AD_RANK.get(v, -1) if v is not None else -1
        )
        filtered = filtered[(rank_series >= lo_rank) & (rank_series <= hi_rank)]
    # U/D Vol range
    udmin, udmax = ud_vol_range
    if udmin > 0.0 or udmax < 5.0:
        udv = filtered["UD_Vol_Ratio"].fillna(0)
        filtered = filtered[(udv >= udmin) & (udv <= udmax)]
    # Ticker/sector/industry search
    if search_q:
        s = search_q.upper()
        mask = filtered["Ticker"].astype(str).str.upper().str.contains(s, na=False)
        if "Sector" in filtered.columns:
            mask = mask | filtered["Sector"].astype(str).str.upper().str.contains(s, na=False)
        if "Industry" in filtered.columns:
            mask = mask | filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)
        filtered = filtered[mask]

    st.session_state["quality_filtered_df"] = filtered

    if len(filtered) != len(df):
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{cyan};"
            f"margin-top:0.5rem;margin-bottom:0.5rem'>"
            f"Showing <b>{len(filtered):,}</b> of <b>{len(df):,}</b> stocks after filters."
            f"</div>",
            unsafe_allow_html=True,
        )

    if len(filtered) == 0:
        st.warning("No stocks match the current filters. Loosen one of the sliders above.")
        return

    # ---- Format numeric columns for display ----
    show = filtered.copy()
    show["Trailing_PE"] = show["Trailing_PE"].apply(lambda v: round(v, 1) if pd.notna(v) else None)
    show["Forward_PE"]  = show["Forward_PE"].apply(lambda v: round(v, 1) if pd.notna(v) else None)
    show["Forward_EPS"] = show["Forward_EPS"].apply(lambda v: round(v, 2) if pd.notna(v) else None)
    show["Fwd_EPS_Growth"] = show["Fwd_EPS_Growth"].apply(lambda v: round(v, 1) if pd.notna(v) else None)
    show["ROE_Pct"] = show["ROE_Pct"].apply(lambda v: round(v, 1) if pd.notna(v) else None)
    show["Inst_Pct"] = show["Inst_Pct"].apply(lambda v: round(v, 1) if pd.notna(v) else None)
    show["UD_Vol_Ratio"] = show["UD_Vol_Ratio"].apply(lambda v: round(v, 2) if pd.notna(v) else None)

    if "Mktcap_Raw" in show.columns:
        show = show.drop(columns=["Mktcap_Raw"])

    col_order = [
        "Ticker", "Sector", "Industry",
        "Composite_Rating", "EPS_Rating",
        "RS_3m", "RS_6m", "Acc_Dis_Rating", "UD_Vol_Ratio",
        "Trailing_PE", "Forward_PE", "Forward_EPS", "Fwd_EPS_Growth",
        "ROE_Pct", "Profit_Margin_Pct", "Inst_Pct",
    ]
    show = show[[c for c in col_order if c in show.columns]]

    column_config = {
        "Ticker":            st.column_config.TextColumn("Ticker", pinned=True, width="small"),
        "Sector":            st.column_config.TextColumn("Sector", width="small"),
        "Industry":          st.column_config.TextColumn("Industry", width="medium"),
        "Composite_Rating":  st.column_config.NumberColumn("Comp", format="%d", help="Composite Rating (1-99): weighted blend of EPS + 6mo RS + Acc/Dis"),
        "EPS_Rating":        st.column_config.NumberColumn("EPS", format="%d", help="EPS Rating (1-99): percentile of forward EPS growth across universe"),
        "RS_3m":             st.column_config.NumberColumn("RS 3m", format="%d", help="3-month price return percentile (1-99)"),
        "RS_6m":             st.column_config.NumberColumn("RS 6m", format="%d", help="6-month price return percentile (1-99)"),
        "Acc_Dis_Rating":    st.column_config.TextColumn("A/D", help="Accumulation/Distribution rating: A+ strongest accumulation, E heaviest distribution"),
        "UD_Vol_Ratio":      st.column_config.NumberColumn("U/D Vol", format="%.2f", help="50-day up-volume / down-volume ratio (>1 = accumulation)"),
        "Trailing_PE":       st.column_config.NumberColumn("PE", format="%.1f", help="Trailing 12-month P/E ratio"),
        "Forward_PE":        st.column_config.NumberColumn("Fwd PE", format="%.1f", help="Forward P/E using next-year EPS estimate"),
        "Forward_EPS":       st.column_config.NumberColumn("Fwd EPS", format="$%.2f", help="Forward EPS estimate"),
        "Fwd_EPS_Growth":    st.column_config.NumberColumn("Fwd Gr %", format="%.1f%%", help="Forward EPS growth: (forward - trailing) / |trailing|"),
        "ROE_Pct":           st.column_config.NumberColumn("ROE %", format="%.1f%%", help="Return on Equity (trailing 12-month)"),
        "Profit_Margin_Pct": st.column_config.NumberColumn("Margin %", format="%.1f%%", help="Profit margin (trailing 12-month)"),
        "Inst_Pct":          st.column_config.NumberColumn("Inst %", format="%.1f%%", help="Institutional ownership %  (includes mutual funds, hedge funds, pension funds, ETFs)"),
    }

    selection = st.dataframe(
        show,
        width='stretch',
        height=600,
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
        key="quality_table_selection",
    )

    # Handle row selection -> dispatch to deep-dive page.
    # Streamlit shows a checkbox in each row when selection_mode is set;
    # clicking it triggers a rerun with the selected row index in `selection`.
    if selection and "selection" in selection and selection["selection"].get("rows"):
        selected_row_idx = selection["selection"]["rows"][0]
        ticker = show.iloc[selected_row_idx]["Ticker"]
        st.session_state.deep_dive_ticker = ticker
        st.session_state.view = "deepdive"
        # Remember that we came from quality so Back-to-scan returns here
        st.session_state.deep_dive_origin = "quality"
        st.rerun()

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.7rem;color:{muted};"
        f"margin-top:1rem;line-height:1.5'>"
        f"<b>Notes:</b> Composite, EPS Rating, and Acc/Dis are proxies — IBD's exact "
        f"algorithms are proprietary. Our proxies rank stocks similarly but won't match "
        f"IBD's numbers byte-for-byte. U/D Vol Ratio, RS 3m, RS 6m, PE, Fwd PE, and "
        f"Forward EPS Growth match IBD's calculations exactly. Inst % is the closest "
        f"available approximation of IBD's Mutual Fund %.</div>",
        unsafe_allow_html=True,
    )
