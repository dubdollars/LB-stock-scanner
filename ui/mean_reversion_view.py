"""
Mean Reversion mode UI — three counter-trend detectors on Quality stocks.

Sub-views (toggle buttons inside the page):
    1. RSI 14            — extreme oversold in long-term uptrend
    2. Connors RSI 2     — very-short-term oversold (Larry Connors)
    3. Pullback to MA    — Stage 2 stock pulling back to 21EMA on light volume

The scan itself runs all three detectors per ticker; the view toggle only
filters the rendered table. Re-toggling between views is instant.
"""
from __future__ import annotations

import re
from datetime import datetime
import pandas as pd
import streamlit as st

from scanner.mean_reversion import (
    run_mean_reversion_scan,
    load_latest_mr_scan,
    DEFAULT_RSI14_THRESHOLD,
    DEFAULT_RSI2_THRESHOLD,
    DEFAULT_PULLBACK_TOLERANCE_PCT,
    DEFAULT_MIN_COMPOSITE,
)
from scanner.system_helpers import keep_awake, allow_sleep
from .style import PALETTE, panel
from .excel_export import format_dataframe_to_excel


# ---------------------------------------------------------------------------
# Hover-card tile docs (same visual pattern as Quality / Momentum tiles)
# ---------------------------------------------------------------------------

_MR_METRIC_DOCS = {
    "UNIVERSE": (
        "**Quality stocks meeting the Composite Rating floor.**\n\n"
        "Mean reversion runs only on stocks that already passed quality filters — "
        "the falling-knife problem (broken stocks don't reliably bounce) is avoided "
        "by gating on Composite Rating.\n\n"
        f"Default floor: ≥ {DEFAULT_MIN_COMPOSITE}. Larger universe = more potential "
        "candidates but more noise."
    ),
    "RSI 14 OVERSOLD": (
        "**Stocks with RSI(14) < threshold AND above their 200-day MA.**\n\n"
        f"Default threshold: {DEFAULT_RSI14_THRESHOLD:.0f}. The classic 'extreme "
        "oversold' setup — statistically reverts within 3-7 days on quality names.\n\n"
        "**How to use:**\n"
        "- Sort by RSI ascending (most oversold first)\n"
        "- Confirm chart shows a single bad week, not a multi-week decline\n"
        "- Entry: market open the next day or on intraday strength\n"
        "- Stop: below the recent low (typically 1-2 ATR)\n"
        "- Exit: RSI(14) back above 50 OR 5-7 day time stop"
    ),
    "CONNORS RSI 2": (
        "**Larry Connors' very-short-term oversold setup.**\n\n"
        f"RSI(2) < {DEFAULT_RSI2_THRESHOLD:.0f}, price > 200-day MA. Hold 1-3 days; "
        "exit when RSI(2) crosses back above 70 OR price reclaims its 5-day SMA.\n\n"
        "**Trade-offs:**\n"
        "- More frequent signals (~2-5× more than RSI 14 setup)\n"
        "- Smaller targets per trade (~0.5-1.5R typical)\n"
        "- Requires faster execution and disciplined exits\n"
        "- Connors' research: ~70% hit rate on SPY-style universes"
    ),
    "PULLBACK TO MA": (
        "**Stage 2 stock pulling back to its 21-day EMA on light volume.**\n\n"
        "Stack required: 21EMA > 50MA > 200MA (textbook Minervini Stage 2). Price within "
        f"±{DEFAULT_PULLBACK_TOLERANCE_PCT:.1f}% of 21EMA. 5-day avg volume < 80% of "
        "50-day avg (pullback on dry volume = healthy; heavy volume = distribution).\n\n"
        "**Why this works:**\n"
        "- 21EMA acts as dynamic support for trending names\n"
        "- Light volume = no institutional selling, just lack of buyers\n"
        "- Often the lowest-risk entry in a strong uptrend"
    ),
    "LAST SCAN": (
        "**Timestamp + age of the most recent Mean Reversion scan.**\n\n"
        "Mean reversion scans use the Quality scan as their universe AND the price "
        "cache for indicators. To refresh both, run a Quality scan first, then a "
        "Mean Reversion scan.\n\n"
        "Re-run intraday for fresh end-of-day signals; mean-reversion setups change "
        "fastest of any scan in the app."
    ),
}


def _md_to_tooltip_html(md: str) -> str:
    """Tiny markdown → HTML for hover tooltips (mirrors quality_view helper)."""
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


def _scan_age_string(iso_str: str) -> str:
    """Format scan age as 'Xm old' / 'Xh old' / 'Xd old'."""
    from datetime import datetime as _dt, timezone as _tz
    if not iso_str:
        return ""
    try:
        s = iso_str
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        delta = _dt.now(_tz.utc) - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m old"
        if hours < 48:
            return f"{int(hours)}h old"
        return f"{int(hours / 24)}d old"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------------

def _render_mr_tiles(scan: dict):
    """5 hover-card tiles: Universe, 3 detector counts, Last Scan age."""
    universe_size = scan.get("universe_size", 0)
    diag = scan.get("diagnostics") or {}
    rsi14_n   = diag.get("rsi14_signal_count", 0)
    connors_n = diag.get("connors_signal_count", 0)
    pullback_n = diag.get("pullback_signal_count", 0)
    age = _scan_age_string(scan.get("timestamp", ""))

    tiles = [
        ("UNIVERSE",         f"{universe_size:,}",   None,                "neutral"),
        ("RSI 14 OVERSOLD",  f"{rsi14_n:,}",          ("↑ Quality oversold" if rsi14_n else "—"),  ("up" if rsi14_n else "neutral")),
        ("CONNORS RSI 2",    f"{connors_n:,}",        ("↑ Very short-term" if connors_n else "—"), ("up" if connors_n else "neutral")),
        ("PULLBACK TO MA",   f"{pullback_n:,}",       ("↑ Light-vol dip" if pullback_n else "—"),  ("up" if pullback_n else "neutral")),
        ("LAST SCAN",        age or "—",              None,                                          "neutral"),
    ]

    css = """
<style>
.mr-grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.8rem 0;
}
.mr-card {
    position: relative;
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: center;
    cursor: help;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.mr-card:hover {
    border-color: #00D4FF;
    box-shadow: 0 0 0 1px rgba(0,212,255,0.25);
}
.mr-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.mr-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
.mr-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    margin-top: 0.25rem;
    font-weight: 600;
}
.mr-delta.up   { color: #00FF88; }
.mr-delta.neutral { color: #8B95A5; }
.mr-tooltip {
    position: absolute;
    top: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
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
.mr-card:hover .mr-tooltip {
    opacity: 1;
    visibility: visible;
}
.mr-tooltip-title {
    font-family: 'JetBrains Mono', monospace;
    color: #00D4FF;
    font-size: 0.85rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
    letter-spacing: 0.04em;
}
.mr-grid .mr-card:nth-child(4) .mr-tooltip,
.mr-grid .mr-card:nth-child(5) .mr-tooltip {
    left: auto; right: 0; transform: none;
}
.mr-grid .mr-card:nth-child(1) .mr-tooltip,
.mr-grid .mr-card:nth-child(2) .mr-tooltip {
    left: 0; transform: none;
}
</style>
"""
    cards = []
    for label, value, delta, delta_class in tiles:
        delta_html = (
            f"<div class='mr-delta {delta_class}'>{delta}</div>"
            if delta is not None else "<div class='mr-delta'>&nbsp;</div>"
        )
        doc_html = _md_to_tooltip_html(_MR_METRIC_DOCS.get(label, ""))
        cards.append(
            f"<div class='mr-card'>"
            f"<div class='mr-label'>{label}</div>"
            f"<div class='mr-value'>{value}</div>"
            f"{delta_html}"
            f"<div class='mr-tooltip'>"
            f"<div class='mr-tooltip-title'>&#9656; {label}</div>"
            f"{doc_html}"
            f"</div></div>"
        )
    st.markdown(css + f"<div class='mr-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Table of Contents dialog
# ---------------------------------------------------------------------------

@st.dialog("Mean Reversion — Table of Contents", width="large")
def _mr_toc_dialog():
    """Modal explaining the three detectors + the columns + how to trade them."""

    def hdr(text):
        st.markdown(
            f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;"
            f"border-radius:4px;font-weight:700;margin-top:0.8rem;margin-bottom:0.3rem'>"
            f"{text}</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.6rem;border-radius:4px'>"
        f"<b>What this scan does:</b> three counter-trend setups looking for stocks "
        f"that have moved too far down (mean reversion) but are still in a long-term "
        f"uptrend. Only runs on Quality-filtered stocks (Composite ≥ {DEFAULT_MIN_COMPOSITE}) "
        f"to avoid the falling-knife trap — oversold quality reverts; oversold junk "
        f"keeps falling."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("Detector 1: RSI 14 Oversold")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Signal:</b> RSI(14) below {DEFAULT_RSI14_THRESHOLD:.0f} AND price above 200-day MA.<br><br>"
        f"<b>Hold:</b> 3-7 days. Exit when RSI(14) reclaims 50 OR on time stop.<br><br>"
        f"<b>Best for:</b> stocks that had one bad week (earnings miss, news event) "
        f"but the long-term trend is intact. RSI 13-15 readings are very extreme — "
        f"those are usually the cleanest setups.<br><br>"
        f"<b>What to check on the chart:</b> single drop (not multi-week decline), "
        f"50/200MA still rising, prior price action shows the stock has bounced "
        f"from oversold readings before."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("Detector 2: Connors RSI 2")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Signal:</b> RSI(2) below {DEFAULT_RSI2_THRESHOLD:.0f} AND price above 200-day MA.<br><br>"
        f"<b>Hold:</b> 1-3 days. Exit when RSI(2) crosses back above 70 OR price "
        f"reclaims its 5-day SMA.<br><br>"
        f"<b>Best for:</b> very-short-term scalp-style trades. RSI(2) is hyper-sensitive "
        f"— readings under 5 happen often, but typically only persist for 1-2 days.<br><br>"
        f"<b>Risk:</b> Connors' research (2008-era) shows ~70% hit rate on SPY-style "
        f"universes, but win sizes are small. Disciplined exits matter more than "
        f"the entry — let one runner turn into a loser and your edge evaporates."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("Detector 3: Pullback to 21EMA")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Signal (ALL required):</b><br>"
        f"&nbsp;&bull; Stage 2 stack: 21EMA &gt; 50MA &gt; 200MA, price above 200MA<br>"
        f"&nbsp;&bull; Price within ±{DEFAULT_PULLBACK_TOLERANCE_PCT:.1f}% of 21EMA<br>"
        f"&nbsp;&bull; 5-day avg volume &lt; 80% of 50-day avg (volume drying up)<br><br>"
        f"<b>Hold:</b> 5-15 days. Often becomes a swing trade if the resumption fires.<br><br>"
        f"<b>Best for:</b> strong trending names that just took a healthy breather. "
        f"This is the LOWEST-risk setup in the tab — you're buying support in a "
        f"confirmed uptrend with no signs of distribution.<br><br>"
        f"<b>Compare to your Journal:</b> overlaps with the 'Pullback to 50MA' "
        f"setup tag. Use 21EMA as the tighter dynamic-support level; 50MA as the "
        f"deeper-pullback fallback."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("Common gates (apply to all three)")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"<b>Above 200MA:</b> long-term uptrend intact. Prevents falling-knife bounces.<br>"
        f"<b>Composite ≥ {DEFAULT_MIN_COMPOSITE}:</b> stock already passed Quality "
        f"fundamentals filter. No junk in this universe.<br><br>"
        f"<b>Why these gates matter:</b> mean reversion only works when the underlying "
        f"thing being reverted-to is healthy. A broken stock at RSI 10 might keep going to "
        f"RSI 5; a healthy stock at RSI 10 almost always bounces."
        f"</div>",
        unsafe_allow_html=True,
    )

    hdr("How to use the page")
    st.markdown(
        f"<div style='color:black;background:#fff;padding:0.4rem 0.6rem;border-radius:4px'>"
        f"1. Click <b>▶ Run Scan</b> in the sidebar (takes ~10-30 sec — uses cached prices).<br>"
        f"2. Toggle which detector view to inspect (three buttons at the top of the table area).<br>"
        f"3. Sort by the column that matters most for that detector (RSI ascending for RSI views, "
        f"Pct From 21EMA absolute-value ascending for Pullback).<br>"
        f"4. Click any row to deep-dive the chart — confirm the setup visually before trading.<br>"
        f"5. Log entries in the Journal under the appropriate tag (RSI oversold, Pullback to 50MA, etc.)."
        f"</div>",
        unsafe_allow_html=True,
    )

    if st.button("Close", use_container_width=True, key="mr_toc_close"):
        st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_mean_reversion_sidebar():
    """Sidebar for Mean Reversion mode."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem;line-height:1.2'>🔁 LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem'>MEAN REVERSION</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.0rem'>"
        f"Counter-trend setups on quality stocks. Three detectors, one tab.</div>",
        unsafe_allow_html=True,
    )

    latest = load_latest_mr_scan()

    # ---- Run Scan ----
    if st.button("▶ Run Scan", key="mr_start_btn", type="primary",
                 use_container_width=True,
                 help="Runs all three detectors against the latest Quality scan "
                      "(Composite ≥ 70). Takes ~10-30 sec — uses cached prices."):
        _run_mr_scan_with_progress()
        st.rerun()

    # ---- Downloads + TOC (only when we have data) ----
    if latest and (latest.get("candidates") or []):
        filtered_df = st.session_state.get("mr_filtered_df")
        if filtered_df is None or len(filtered_df) == 0:
            filtered_df = pd.DataFrame(latest["candidates"])

        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                filtered_df, sheet_name="Mean Reversion",
                title=f"Legacy Brands - Mean Reversion {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"mean_reversion_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
                key="mr_excel_btn",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl`.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")

        try:
            tickers_text = "\n".join(filtered_df["Ticker"].astype(str).tolist())
            stamp = datetime.utcnow().strftime("%d%b%Y")
            st.download_button(
                label="📥 Download Tickers (.txt)",
                data=tickers_text,
                file_name=f"mr_tickers_{stamp}.txt",
                mime="text/plain",
                use_container_width=True,
                type="primary",
                key="mr_tickers_btn",
            )
        except Exception as e:
            st.caption(f"Tickers export error: {e}")

        if st.button("📖 Table of Contents", type="primary",
                     use_container_width=True, key="mr_toc_btn",
                     help="Detector explanations, column definitions, how to trade each setup"):
            _mr_toc_dialog()

        st.caption(f"Exports include the {len(filtered_df):,} rows currently visible.")
    else:
        st.caption("Run a Mean Reversion scan to enable exports.")

    st.markdown("---")

    # ---- Last Scan info ----
    if latest:
        diag = latest.get("diagnostics") or {}
        ts = latest.get("timestamp", "")
        ts_display = ts.split(".")[0].replace("T", " ").replace("Z", "") + " UTC"
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
            f"line-height:1.5'>"
            f"<b style='color:{cyan}'>LAST SCAN</b><br>"
            f"{ts_display}<br>"
            f"Universe: {latest.get('universe_size', 0):,} stocks<br>"
            f"RSI 14 signals: {diag.get('rsi14_signal_count', 0):,}<br>"
            f"Connors signals: {diag.get('connors_signal_count', 0):,}<br>"
            f"Pullback signals: {diag.get('pullback_signal_count', 0):,}"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
            f"margin-top:0.25rem'>No Mean Reversion scan yet — run one above. "
            f"Requires a recent Quality scan as its universe.</div>",
            unsafe_allow_html=True,
        )


def _run_mr_scan_with_progress():
    """Run the scan with a sidebar progress bar. Wrapped in keep_awake/allow_sleep
    for consistency with the other scans — MR is usually 10-30 sec but the
    sleep override is cheap insurance against an unexpectedly slow run."""
    bar = st.progress(0.0, text="Mean Reversion scan starting…")

    def _prog(stage, completed, total, msg):
        pct = (completed / max(1, total)) if total else 0.0
        bar.progress(min(1.0, pct), text=f"[MR · {stage.upper()}] {msg}")

    keep_awake()
    try:
        result = run_mean_reversion_scan(progress=_prog)
        bar.progress(1.0, text=f"Mean Reversion scan complete — {len(result.candidates)} candidates")
    except Exception as e:
        st.error(f"Mean Reversion scan failed: {e}")
    finally:
        allow_sleep()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

# Column display per sub-view — each view highlights the columns most relevant
# to its detector type. Ticker is always pinned + leftmost.
_VIEW_COLUMNS = {
    "rsi14": [
        "Ticker", "Price", "RSI_14", "RSI_2", "Pct_Above_200MA",
        "Composite_Rating", "EPS_Rating", "Sector", "Market_Cap", "Industry",
    ],
    "connors": [
        "Ticker", "Price", "RSI_2", "RSI_14", "Pct_Above_200MA",
        "Composite_Rating", "EPS_Rating", "Sector", "Market_Cap", "Industry",
    ],
    "pullback": [
        "Ticker", "Price", "Pct_From_21EMA", "Pct_From_50MA", "Vol_Drying",
        "Stage2_Stack", "RSI_14", "Composite_Rating", "Sector", "Market_Cap", "Industry",
    ],
}

_VIEW_SIGNAL_KEY = {
    "rsi14":    "RSI14_Signal",
    "connors":  "Connors_Signal",
    "pullback": "Pullback_Signal",
}

_VIEW_LABEL = {
    "rsi14":    "📉 RSI 14 OVERSOLD",
    "connors":  "⚡ CONNORS RSI 2",
    "pullback": "📊 PULLBACK TO MA",
}


def render_mean_reversion_page():
    """Render the Mean Reversion dashboard — tiles + view switcher + table."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<h2 style='color:{cyan};margin-top:0'>🔁 Mean Reversion</h2>"
        f"<div style='color:{muted};font-size:0.9rem;line-height:1.5;margin-bottom:1rem;max-width:900px'>"
        f"Three counter-trend setups looking for stocks that moved too far down but "
        f"are still in long-term uptrends. Quality-filtered (Composite ≥ {DEFAULT_MIN_COMPOSITE}) "
        f"to avoid the falling-knife trap. Toggle between detector views below."
        f"</div>",
        unsafe_allow_html=True,
    )

    with st.expander("ℹ️ Why this page exists (click for the rationale)", expanded=False):
        st.markdown(
            "**Mean reversion is the opposite edge from momentum.** Momentum buys "
            "strength near pivots; mean reversion buys *weakness* that is statistically "
            "likely to revert. It works in choppy/sideways tape — exactly when momentum "
            "fails — and gives more frequent trades than waiting for VCP breakouts."
        )
        st.markdown(
            "**The Quality filter is non-negotiable.** Oversold *broken* stocks don't "
            "reliably bounce — they often keep falling. Oversold *strong* stocks (high "
            "Composite Rating, real earnings, institutional sponsorship) revert because "
            "long-term holders defend their positions. Gating on Composite ≥ 70 means "
            "every candidate here has already passed a fundamental quality check."
        )
        st.markdown(
            "**The 200MA gate is non-negotiable.** Below the 200MA, you're trying to "
            "catch a falling knife in a structural downtrend. Above the 200MA, you're "
            "buying a temporary dip in an established uptrend. Same RSI reading, "
            "completely different trade."
        )
        st.markdown(
            "**Three detectors, three trade horizons.** RSI 14 = 3-7 day swing. Connors "
            "RSI 2 = 1-3 day scalp. Pullback to MA = 5-15 day swing. Pick the one that "
            "matches your management style; tag the trade in the Journal accordingly."
        )

    latest = load_latest_mr_scan()
    if latest is None:
        st.info(
            "No Mean Reversion scan has been run yet. Use the **▶ Run Scan** button in "
            "the sidebar to populate this page.\n\n"
            "**Prerequisite:** a recent Quality scan must exist (this scanner uses "
            "Quality candidates as its universe). If you haven't run Quality recently, "
            "head to the QUALITY tab first."
        )
        return

    # If the scan errored (e.g. no Quality scan), surface it
    diag = latest.get("diagnostics") or {}
    if diag.get("error"):
        st.error(diag["error"])
        return

    candidates = latest.get("candidates") or []

    # Tiles always render (even when no candidates) so the universe count + age are visible
    _render_mr_tiles(latest)

    if not candidates:
        st.warning(
            "Latest Mean Reversion scan returned 0 candidates. This usually means the "
            "market is broadly bullish (few oversold names) or the Quality universe is "
            "small. Try running a fresh Quality scan, then re-running this scan."
        )
        return

    # ---- View switcher (3 toggle buttons) ----
    if "mr_view" not in st.session_state:
        st.session_state["mr_view"] = "rsi14"

    def _vbtn_type(view: str) -> str:
        return "primary" if st.session_state["mr_view"] == view else "secondary"

    vc1, vc2, vc3, vc_spacer = st.columns([1.6, 1.6, 1.6, 4.5])
    with vc1:
        if st.button(_VIEW_LABEL["rsi14"], type=_vbtn_type("rsi14"),
                     use_container_width=True, key="mr_view_rsi14"):
            st.session_state["mr_view"] = "rsi14"
            st.rerun()
    with vc2:
        if st.button(_VIEW_LABEL["connors"], type=_vbtn_type("connors"),
                     use_container_width=True, key="mr_view_connors"):
            st.session_state["mr_view"] = "connors"
            st.rerun()
    with vc3:
        if st.button(_VIEW_LABEL["pullback"], type=_vbtn_type("pullback"),
                     use_container_width=True, key="mr_view_pullback"):
            st.session_state["mr_view"] = "pullback"
            st.rerun()

    active_view = st.session_state["mr_view"]
    signal_key = _VIEW_SIGNAL_KEY[active_view]

    df = pd.DataFrame(candidates)

    # Filter to the active detector's signal
    if signal_key in df.columns:
        view_df = df[df[signal_key] == "YES"].copy()
    else:
        view_df = df.copy()

    if view_df.empty:
        st.info(f"No candidates fired the **{_VIEW_LABEL[active_view]}** signal in the "
                f"latest scan. Try a different view, or re-run if the scan is stale.")
        return

    # ---- Filter row: Sector | Search | (per-view narrowing slider) ----
    sectors = sorted([s for s in view_df.get("Sector", pd.Series(dtype=str)).dropna().unique().tolist() if s and s != "N/A"])

    fc1, fc2, fc3 = st.columns([1.6, 1.6, 4.0])
    with fc1:
        sector_filter = st.selectbox(
            "Sector", options=["All sectors"] + sectors, index=0,
            key=f"mr_sector_{active_view}",
        )
    with fc2:
        if active_view == "rsi14":
            max_rsi = st.slider(
                "Max RSI(14)", min_value=5, max_value=int(DEFAULT_RSI14_THRESHOLD),
                value=int(DEFAULT_RSI14_THRESHOLD), step=1,
                key="mr_max_rsi14",
                help="Narrow the candidate list by lowering the RSI ceiling. "
                     "Lower = more extreme oversold.",
            )
            max_rsi2 = None
            max_pullback = None
        elif active_view == "connors":
            max_rsi2 = st.slider(
                "Max RSI(2)", min_value=1, max_value=int(DEFAULT_RSI2_THRESHOLD),
                value=int(DEFAULT_RSI2_THRESHOLD), step=1,
                key="mr_max_rsi2",
            )
            max_rsi = None
            max_pullback = None
        else:  # pullback
            max_pullback = st.slider(
                "Max distance from 21EMA (%)",
                min_value=0.5, max_value=float(DEFAULT_PULLBACK_TOLERANCE_PCT),
                value=float(DEFAULT_PULLBACK_TOLERANCE_PCT), step=0.1,
                key="mr_max_pullback",
            )
            max_rsi = None
            max_rsi2 = None
    with fc3:
        search = st.text_input(
            "Search", placeholder="e.g. NVDA or tech",
            key=f"mr_search_{active_view}",
            help="Filter by ticker, sector, or industry (case-insensitive).",
        )

    # ---- Apply filters ----
    filtered = view_df.copy()
    if sector_filter != "All sectors":
        filtered = filtered[filtered["Sector"] == sector_filter]
    if active_view == "rsi14" and max_rsi is not None and "RSI_14" in filtered.columns:
        filtered = filtered[filtered["RSI_14"].fillna(999) <= max_rsi]
    if active_view == "connors" and max_rsi2 is not None and "RSI_2" in filtered.columns:
        filtered = filtered[filtered["RSI_2"].fillna(999) <= max_rsi2]
    if active_view == "pullback" and max_pullback is not None and "Pct_From_21EMA" in filtered.columns:
        filtered = filtered[filtered["Pct_From_21EMA"].fillna(999).abs() <= max_pullback]
    if search:
        s = search.upper()
        mask = filtered["Ticker"].astype(str).str.upper().str.contains(s, na=False)
        if "Sector" in filtered.columns:
            mask = mask | filtered["Sector"].astype(str).str.upper().str.contains(s, na=False)
        if "Industry" in filtered.columns:
            mask = mask | filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)
        filtered = filtered[mask]

    # Default sort per view (most actionable first)
    if active_view == "rsi14" and "RSI_14" in filtered.columns:
        filtered = filtered.sort_values("RSI_14", ascending=True, na_position="last")
    elif active_view == "connors" and "RSI_2" in filtered.columns:
        filtered = filtered.sort_values("RSI_2", ascending=True, na_position="last")
    elif active_view == "pullback" and "Pct_From_21EMA" in filtered.columns:
        # absolute distance ascending — closest to the 21EMA first
        filtered = filtered.iloc[filtered["Pct_From_21EMA"].abs().argsort()]

    st.session_state["mr_filtered_df"] = filtered

    if len(filtered) != len(view_df):
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{cyan};"
            f"margin-top:0.5rem;margin-bottom:0.5rem'>"
            f"Showing <b>{len(filtered):,}</b> of <b>{len(view_df):,}</b> "
            f"<b>{_VIEW_LABEL[active_view]}</b> candidates after filters."
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{cyan};"
            f"margin-top:0.5rem;margin-bottom:0.5rem'>"
            f"<b>{len(filtered):,}</b> <b>{_VIEW_LABEL[active_view]}</b> candidates."
            f"</div>",
            unsafe_allow_html=True,
        )

    # ---- Build display dataframe (only the columns for this view) ----
    cols = _VIEW_COLUMNS[active_view]
    show = filtered[[c for c in cols if c in filtered.columns]].copy()

    column_config = {
        "Ticker":           st.column_config.TextColumn("Ticker", pinned=True, width="small"),
        "Price":            st.column_config.NumberColumn("Price", format="$%,.2f", width="small", alignment="center"),
        "RSI_14":           st.column_config.NumberColumn("RSI 14", format="%.1f", width="small", alignment="center",
                                help="14-period Wilder RSI. <30 = oversold, <20 = extreme, <15 = washout"),
        "RSI_2":            st.column_config.NumberColumn("RSI 2", format="%.1f", width="small", alignment="center",
                                help="2-period RSI (Connors). <5 = oversold, <2 = extreme washout"),
        "Pct_Above_200MA":  st.column_config.NumberColumn("% > 200MA", format="%+.1f%%", width="small", alignment="center",
                                help="Distance above 200-day MA. Positive = uptrend intact."),
        "Pct_From_21EMA":   st.column_config.NumberColumn("% from 21EMA", format="%+.1f%%", width="small", alignment="center",
                                help="Distance from 21-day EMA. Closer to 0 = right at support."),
        "Pct_From_50MA":    st.column_config.NumberColumn("% from 50MA", format="%+.1f%%", width="small", alignment="center",
                                help="Distance from 50-day MA."),
        "Vol_Drying":       st.column_config.TextColumn("Vol Dry", width="small", alignment="center",
                                help="YES = 5-day avg vol < 80% of 50-day avg (healthy pullback)"),
        "Stage2_Stack":     st.column_config.TextColumn("Stage 2", width="small", alignment="center",
                                help="YES = 21EMA > 50MA > 200MA + price > 200MA (Minervini stack confirmed)"),
        "Composite_Rating": st.column_config.NumberColumn("Comp", format="%d", width="small", alignment="center",
                                help="Composite Rating from Quality scan"),
        "EPS_Rating":       st.column_config.NumberColumn("EPS", format="%d", width="small", alignment="center",
                                help="EPS Rating from Quality scan"),
        "Sector":           st.column_config.TextColumn("Sector", width="small"),
        "Industry":         st.column_config.TextColumn("Industry"),
        "Market_Cap":       st.column_config.TextColumn("Mkt Cap", width="small"),
    }

    selection = st.dataframe(
        show,
        width="stretch",
        height=min(640, 90 + len(show) * 36),
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
        key=f"mr_table_{active_view}",
    )

    if selection and "selection" in selection and selection["selection"].get("rows"):
        idx = selection["selection"]["rows"][0]
        ticker = show.iloc[idx]["Ticker"]
        st.session_state.deep_dive_ticker = ticker
        st.session_state.view = "deepdive"
        # Origin dict — deep-dive back button restores Mean Reversion with
        # the same sub-view (RSI2 / Connors / Pullback) still selected.
        st.session_state.deep_dive_origin = {
            "mode": "mean_reversion", "view": "mean_reversion",
            "label": "← Back to Mean Reversion",
        }
        st.rerun()

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.7rem;color:{muted};"
        f"margin-top:1rem;line-height:1.5'>"
        f"<b>Notes:</b> Mean reversion is a counter-trend edge — opposite of momentum. "
        f"Always confirm the chart before trading; a flagged signal is a CANDIDATE, "
        f"not a buy order. Log every trade in the Journal under the appropriate setup tag."
        f"</div>",
        unsafe_allow_html=True,
    )
