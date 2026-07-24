"""
Deep-dive page renderer.

Layout:
  - Header strip (ticker, name, sector, market cap, price + 52w change)
  - 5 tabs: FUNDAMENTALS | SOCIAL | EXECUTION | CHART | RS LINE
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from scanner.deepdive import DeepDiveData, fetch_deep_dive
from .style import PALETTE, panel, pill


# ---------------------------------------------------------------------------
# Cached Reddit fetcher
# ---------------------------------------------------------------------------
#
# Streamlit re-runs the entire script on every interaction and executes the
# body of every tab on every render (even hidden ones), so a raw call to
# fetch_reddit_mentions would fire ~7-14 HTTP requests every time the user
# clicks a sub-tab or scrolls. We wrap the call in @st.cache_data so it
# only re-fetches once per (ticker, limit, time_filter) every 10 minutes.
# That keeps the Chart / Fib / Execution tabs responsive while still giving
# the Social tab fresh-enough data.

@st.cache_data(ttl=600, show_spinner=False)
def _cached_reddit_mentions(ticker: str, limit_per_sub: int, time_filter: str) -> dict:
    """Cached wrapper around scanner.social_scan.fetch_reddit_mentions.
    TTL is 10 minutes — long enough to make tab switches instant, short
    enough that the user sees reasonably fresh discussion."""
    from scanner.social_scan import fetch_reddit_mentions
    return fetch_reddit_mentions(
        ticker, limit_per_sub=limit_per_sub, time_filter=time_filter
    )


# ---------------------------------------------------------------------------
# Plotly base layout
# ---------------------------------------------------------------------------

def _base_layout(title: str = "", height: int = 500) -> dict:
    return dict(
        template="plotly_dark",
        # Open with the Pan tool active by default instead of Zoom — far
        # more intuitive for scrolling through historical price data.
        dragmode="pan",
        plot_bgcolor=PALETTE["bg_main"],
        paper_bgcolor=PALETTE["bg_main"],
        font=dict(family="JetBrains Mono, monospace", color=PALETTE["text_primary"], size=13),
        title=dict(text=title, font=dict(size=15, color=PALETTE["text_muted"]),
                   x=0.01, y=0.98, yanchor="top"),
        # Extra top margin — bumped 70→95 to accommodate the wider gap
        # between title and legend (user request 2026-07-22: they were
        # almost overlapping at t=70 + legend y=1.02).
        margin=dict(l=50, r=140, t=95, b=40),
        height=height,
        xaxis=dict(
            gridcolor=PALETTE["border"],
            zerolinecolor=PALETTE["border"],
            showgrid=True,
            showspikes=True,
            spikemode="across",
            spikecolor=PALETTE["accent_cyan"],
            spikethickness=1,
        ),
        yaxis=dict(
            gridcolor=PALETTE["border"],
            zerolinecolor=PALETTE["border"],
            showgrid=True,
        ),
        # Legend: horizontal, ABOVE the plot area, white text (was inside the
        # chart at top-left in muted color which was hard to read).
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            bordercolor=PALETTE["border"],
            borderwidth=0,
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=13, color="#ffffff"),
        ),
        hoverlabel=dict(
            bgcolor=PALETTE["bg_panel"],
            bordercolor=PALETTE["accent_cyan"],
            font=dict(family="JetBrains Mono, monospace"),
        ),
    )


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _build_price_chart(d: DeepDiveData) -> go.Figure:
    h = d.history.copy()
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.78, 0.22],
        subplot_titles=("", ""),
    )

    # Candlesticks
    fig.add_trace(
        go.Candlestick(
            x=h.index,
            open=h["Open"], high=h["High"], low=h["Low"], close=h["Close"],
            increasing=dict(line=dict(color=PALETTE["up_green"]), fillcolor=PALETTE["up_green"]),
            decreasing=dict(line=dict(color=PALETTE["down_red"]), fillcolor=PALETTE["down_red"]),
            name="Price",
            showlegend=False,
        ),
        row=1, col=1,
    )

    # SMAs — colors set per user request 2026-07-22:
    # SMA 50 = blue, SMA 150 = gold/yellow, SMA 200 = red.
    # (Previously SMA 50 was gold, 150 cyan, 200 purple — reordered so the
    # shortest MA is the calm/blue color and the longest is the alerting red,
    # matching how the user reads trend health from the chart.)
    for col, color, name in [
        ("SMA50",  "#3B82F6", "SMA 50"),   # blue
        ("SMA150", "#F5B841", "SMA 150"),  # gold/yellow
        ("SMA200", "#EF4444", "SMA 200"),  # red
    ]:
        fig.add_trace(
            go.Scatter(
                x=h.index, y=h[col], name=name,
                line=dict(color=color, width=1.3),
                hovertemplate=f"{name}: %{{y:.2f}}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Buy trigger + stop horizontal lines
    fig.add_hline(
        y=d.buy_trigger, line_dash="dash", line_color=PALETTE["up_green"],
        annotation_text=f"BUY {d.buy_trigger:.2f}",
        annotation_position="right", annotation_font=dict(color=PALETTE["up_green"], size=12),
        row=1, col=1,
    )
    fig.add_hline(
        y=d.natural_stop, line_dash="dash", line_color=PALETTE["down_red"],
        annotation_text=f"STOP {d.natural_stop:.2f}",
        annotation_position="right", annotation_font=dict(color=PALETTE["down_red"], size=12),
        row=1, col=1,
    )

    # VCP zones — vertical rectangles with readable labels.
    # White bold text on a dark semi-transparent pill keeps the labels
    # legible against the cyan-tinted zone fill (the old cyan-on-cyan
    # had basically no contrast). Alternating top/bottom positions
    # also prevents overlapping labels when contractions are close
    # together in time.
    if d.vcp_zones:
        for i, (start_dt, end_dt, contraction) in enumerate(d.vcp_zones):
            # Alternate label position to reduce stacking on overlapping zones
            pos = "top left" if i % 2 == 0 else "bottom left"
            fig.add_vrect(
                x0=start_dt, x1=end_dt,
                fillcolor=PALETTE["accent_cyan"], opacity=0.10,
                line_width=0,
                annotation_text=f"VCP -{contraction:.1f}%",
                annotation_position=pos,
                annotation_font=dict(color="#FFFFFF", size=12, family="JetBrains Mono, monospace"),
                annotation_bgcolor="rgba(15, 22, 32, 0.85)",
                annotation_bordercolor=PALETTE["accent_cyan"],
                annotation_borderwidth=1,
                annotation_borderpad=3,
                row=1, col=1,
            )

    # Volume bars (color by up/down day)
    colors = [
        PALETTE["up_green"] if c >= o else PALETTE["down_red"]
        for o, c in zip(h["Open"], h["Close"])
    ]
    fig.add_trace(
        go.Bar(
            x=h.index, y=h["Volume"],
            marker_color=colors,
            opacity=0.7,
            name="Volume",
            showlegend=False,
            hovertemplate="Vol: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    layout = _base_layout(title="PRICE  •  SMAs  •  VCP ZONES  •  BUY/STOP", height=800)
    layout["xaxis"]["rangeslider"] = dict(visible=False)
    layout["xaxis2"] = dict(gridcolor=PALETTE["border"])
    layout["yaxis2"] = dict(gridcolor=PALETTE["border"], title="Volume")
    fig.update_layout(**layout)
    return fig


def _build_rs_line_chart(d: DeepDiveData) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=d.rs_line.index, y=d.rs_line.values,
            mode="lines",
            line=dict(color=PALETTE["accent_cyan"], width=2),
            name="RS Line",
            hovertemplate="RS: %{y:.4f}<extra></extra>",
        )
    )
    # New-high markers
    if d.rs_new_high_dates:
        marker_y = [d.rs_line.loc[dt] for dt in d.rs_new_high_dates if dt in d.rs_line.index]
        marker_x = [dt for dt in d.rs_new_high_dates if dt in d.rs_line.index]
        if marker_x:
            fig.add_trace(
                go.Scatter(
                    x=marker_x, y=marker_y,
                    mode="markers",
                    marker=dict(color=PALETTE["up_green"], size=8, symbol="triangle-up",
                                line=dict(color=PALETTE["text_primary"], width=1)),
                    name="New 50d High",
                    hovertemplate="NEW HIGH<br>%{x|%Y-%m-%d}<br>RS: %{y:.4f}<extra></extra>",
                )
            )
    fig.update_layout(**_base_layout(title=f"RELATIVE STRENGTH LINE  ({d.ticker} / SPY)", height=400))
    return fig


def _build_eps_chart(d: DeepDiveData) -> go.Figure:
    df = d.eps_history.copy()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if not df.empty:
        x = [str(q)[:10] for q in df["quarter"]]
        bar_colors = [
            PALETTE["up_green"] if v >= 0 else PALETTE["down_red"]
            for v in df["eps"]
        ]
        fig.add_trace(go.Bar(
            x=x, y=df["eps"], name="Net Income",
            marker_color=bar_colors,
            hovertemplate="Q: %{x}<br>NI: $%{y:,.0f}<extra></extra>",
        ), secondary_y=False)
        fig.add_trace(go.Scatter(
            x=x, y=df["yoy_pct"], name="YoY %",
            mode="lines+markers",
            line=dict(color=PALETTE["accent_cyan"], width=2),
            marker=dict(size=7),
            hovertemplate="YoY: %{y:.1f}%<extra></extra>",
        ), secondary_y=True)
    layout = _base_layout(title="QUARTERLY NET INCOME  +  YoY GROWTH %", height=350)
    layout.pop("yaxis", None)
    fig.update_layout(**layout)
    fig.update_yaxes(title_text="Net Income ($)", gridcolor=PALETTE["border"], secondary_y=False)
    fig.update_yaxes(title_text="YoY %", gridcolor="rgba(0,0,0,0)", secondary_y=True)
    return fig


def _build_revenue_chart(d: DeepDiveData) -> go.Figure:
    df = d.revenue_history.copy()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if not df.empty:
        x = [str(q)[:10] for q in df["quarter"]]
        fig.add_trace(go.Bar(
            x=x, y=df["revenue"], name="Revenue",
            marker_color=PALETTE["accent_cyan"], opacity=0.7,
            hovertemplate="Q: %{x}<br>Rev: $%{y:,.0f}<extra></extra>",
        ), secondary_y=False)
        fig.add_trace(go.Scatter(
            x=x, y=df["yoy_pct"], name="YoY %",
            mode="lines+markers",
            line=dict(color=PALETTE["up_green"], width=2),
            marker=dict(size=7),
            hovertemplate="YoY: %{y:.1f}%<extra></extra>",
        ), secondary_y=True)
    layout = _base_layout(title="QUARTERLY REVENUE  +  YoY GROWTH %", height=350)
    layout.pop("yaxis", None)
    fig.update_layout(**layout)
    fig.update_yaxes(title_text="Revenue ($)", gridcolor=PALETTE["border"], secondary_y=False)
    fig.update_yaxes(title_text="YoY %", gridcolor="rgba(0,0,0,0)", secondary_y=True)
    return fig


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _render_header(d: DeepDiveData):
    pct = d.pct_from_52w_high
    pct_color = "up_green" if pct >= -5 else ("signal_yellow" if pct >= -15 else "down_red")
    pct_pill = pill(f"{pct:+.1f}% from 52w high", pct_color.replace("up_", "").replace("signal_", ""))

    col1, col2, col3, col4, col5 = st.columns([2, 2, 2, 2, 3])
    with col1:
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:1.8rem;font-weight:700;color:{PALETTE['accent_cyan']}'>{d.ticker}</div>"
            f"<div style='font-size:0.85rem;color:{PALETTE['text_muted']}'>{d.company_name[:40]}</div>",
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            f"<div class='panel-title'>PRICE</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.5rem;font-weight:700'>${d.current_price:,.2f}</div>"
            f"<div>{pct_pill}</div>",
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            f"<div class='panel-title'>MARKET CAP</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.2rem;font-weight:600'>{d.market_cap}</div>"
            f"<div style='font-size:0.8rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>{d.sector}</div>",
            unsafe_allow_html=True,
        )
    with col4:
        rs_text = f"{d.rs_rating}" if d.rs_rating is not None else "N/A"
        rsi_text = f"{d.rsi_14:.1f}" if d.rsi_14 is not None else "N/A"
        st.markdown(
            f"<div class='panel-title'>RS / RSI</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.2rem;font-weight:600'>"
            f"<span style='color:{PALETTE['accent_cyan']}'>{rs_text}</span> &nbsp; / &nbsp; {rsi_text}</div>"
            f"<div style='font-size:0.75rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>RS estimated • RSI 14d</div>",
            unsafe_allow_html=True,
        )
    with col5:
        ee = d.next_earnings_date or "—"
        st.markdown(
            f"<div class='panel-title'>NEXT EARNINGS</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.2rem;font-weight:600'>{ee}</div>"
            f"<div style='font-size:0.8rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>{d.industry[:50]}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("---")


# ---------------------------------------------------------------------------
# Tab content
# ---------------------------------------------------------------------------

def _add_fib_lines_to_fig(fig, fib_data, show_retracements: bool, show_extensions: bool):
    """Mutate a Plotly fig in place — add Fibonacci retracement and/or
    extension horizontal lines to the price subplot."""
    if not fib_data:
        return
    retracement_color = "#ffd166"   # warm gold — distinct from existing SMAs
    extension_color = "#c084fc"     # light purple — clearly different from gold

    if show_retracements:
        for r in fib_data.get("retracements", []):
            fig.add_hline(
                y=r["price"],
                line_dash="dot",
                line_color=retracement_color,
                line_width=1,
                opacity=0.65,
                annotation_text=f"Fib {r['label']}  {r['price']:.2f}",
                annotation_position="right",
                annotation_font=dict(color=retracement_color, size=11),
                row=1, col=1,
            )
    if show_extensions:
        for e in fib_data.get("extensions", []):
            fig.add_hline(
                y=e["price"],
                line_dash="dot",
                line_color=extension_color,
                line_width=1,
                opacity=0.6,
                annotation_text=f"Ext {e['label']}  {e['price']:.2f}",
                annotation_position="right",
                annotation_font=dict(color=extension_color, size=11),
                row=1, col=1,
            )


def _render_fib_summary(d, fib_data, show_retracements: bool, show_extensions: bool):
    """Render a small data table below the price chart summarising the Fib
    levels and their distance from the current price. Highlights whichever
    level is closest to current price."""
    if not fib_data:
        st.caption(
            "Fibonacci levels couldn't be computed (need at least 30 sessions of data with a real high-to-low range)."
        )
        return

    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    primary = PALETTE["text_primary"]
    gold = "#ffd166"
    purple = "#c084fc"

    current = float(d.history["Close"].iloc[-1]) if len(d.history) else 0.0
    swing_high = fib_data.get("swing_high")
    swing_low = fib_data.get("swing_low")
    swing_high_date = fib_data.get("swing_high_date")
    swing_low_date = fib_data.get("swing_low_date")
    range_pts = fib_data.get("range_pts")

    # Header explaining the swing
    try:
        sh_date_str = swing_high_date.strftime("%Y-%m-%d") if swing_high_date is not None else ""
        sl_date_str = swing_low_date.strftime("%Y-%m-%d") if swing_low_date is not None else ""
    except Exception:
        sh_date_str = sl_date_str = ""

    header_html = (
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;color:{muted};margin-top:0.4rem;margin-bottom:0.5rem'>"
        f"Auto-detected swing &middot; "
        f"<b style='color:{gold}'>HIGH ${swing_high:.2f}</b> ({sh_date_str}) "
        f"&rarr; "
        f"<b style='color:{primary}'>LOW ${swing_low:.2f}</b> ({sl_date_str}) "
        f"&middot; range ${range_pts:.2f} &middot; current price <b style='color:{cyan}'>${current:.2f}</b>"
        f"</div>"
    )
    st.markdown(header_html, unsafe_allow_html=True)

    # Collect all levels we're showing
    levels = []
    if show_retracements:
        for r in fib_data.get("retracements", []):
            levels.append({
                "kind": "Retracement",
                "label": r["label"],
                "price": r["price"],
                "color": gold,
            })
    if show_extensions:
        for e in fib_data.get("extensions", []):
            levels.append({
                "kind": "Extension",
                "label": e["label"],
                "price": e["price"],
                "color": purple,
            })

    if not levels:
        return

    # Find the closest level to current price
    closest_idx = None
    closest_diff = float("inf")
    for i, lvl in enumerate(levels):
        diff = abs(lvl["price"] - current)
        if diff < closest_diff:
            closest_diff = diff
            closest_idx = i

    # Render as an HTML table
    rows_html = []
    for i, lvl in enumerate(levels):
        diff = lvl["price"] - current
        pct_off = (diff / current * 100) if current > 0 else 0
        direction = "above" if diff > 0 else ("below" if diff < 0 else "at")
        is_closest = (i == closest_idx)
        row_bg = f"background:rgba(0,212,255,0.10);" if is_closest else ""
        closest_badge = (
            f"<span style='color:{cyan};font-weight:700;margin-left:0.5rem'>CLOSEST</span>"
            if is_closest else ""
        )
        rows_html.append(
            f"<tr style='{row_bg}'>"
            f"<td style='padding:0.35rem 0.75rem;color:{lvl['color']};font-weight:600'>{lvl['kind']}</td>"
            f"<td style='padding:0.35rem 0.75rem;font-family:JetBrains Mono;color:{lvl['color']}'>{lvl['label']}</td>"
            f"<td style='padding:0.35rem 0.75rem;text-align:right;font-family:JetBrains Mono;color:{primary}'>${lvl['price']:.2f}</td>"
            f"<td style='padding:0.35rem 0.75rem;text-align:right;font-family:JetBrains Mono;color:{muted}'>{pct_off:+.2f}% {direction}</td>"
            f"<td style='padding:0.35rem 0.75rem'>{closest_badge}</td>"
            f"</tr>"
        )
    table_html = (
        f"<table style='width:100%;border-collapse:collapse;font-size:0.85rem;"
        f"background:{PALETTE['bg_panel']};border:1px solid {PALETTE['border']};border-radius:6px;overflow:hidden'>"
        f"<thead><tr style='background:{PALETTE['bg_panel_2']};color:{muted};text-transform:uppercase;font-size:0.7rem;letter-spacing:0.06em'>"
        f"<th style='padding:0.5rem 0.75rem;text-align:left'>Type</th>"
        f"<th style='padding:0.5rem 0.75rem;text-align:left'>Level</th>"
        f"<th style='padding:0.5rem 0.75rem;text-align:right'>Price</th>"
        f"<th style='padding:0.5rem 0.75rem;text-align:right'>vs current</th>"
        f"<th style='padding:0.5rem 0.75rem'></th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


@st.dialog("Fibonacci Explained", width="large")
def _fibonacci_explained_dialog():
    """Clean explanation of Fib levels and how traders use them.

    Important: we use <div> for headings rather than <h3>/<h4> because the
    global app CSS forces h-tags to the dark-theme text color via !important,
    which renders gray on the dialog's white background. <div> escapes that
    rule, letting us hit real black.
    """
    # Colors used sparingly — gold for retracement % labels, purple for
    # extension % labels, black for everything else. Single font (Inter)
    # throughout so the dialog reads like a clean printed page.
    gold = "#b8860b"
    purple = "#6b21a8"

    # One big HTML block — single style pass, single font, full control of
    # spacing and color. No mixing st.markdown() heading styles with our own.
    html = f"""
    <style>
      .fib-dialog {{
        font-family: 'Inter', -apple-system, system-ui, sans-serif;
        color: #000000 !important;
        line-height: 1.6;
        font-size: 0.95rem;
      }}
      .fib-dialog * {{ color: #000000 !important; }}
      .fib-dialog .heading {{
        font-size: 1.2rem;
        font-weight: 700;
        margin-top: 1.4rem;
        margin-bottom: 0.55rem;
        color: #000000 !important;
      }}
      .fib-dialog .heading:first-child {{ margin-top: 0; }}
      .fib-dialog .formula {{
        font-family: 'Inter', sans-serif;
        font-weight: 600;
        margin: 0.35rem 0 0.6rem 1.25rem;
        font-size: 0.95rem;
        color: #000000 !important;
      }}
      .fib-dialog .group-label {{
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-size: 0.85rem;
        margin-top: 0.9rem;
        margin-bottom: 0.4rem;
      }}
      .fib-dialog .group-label.retracement {{ color: {gold} !important; }}
      .fib-dialog .group-label.extension {{ color: {purple} !important; }}
      .fib-dialog .level-row {{
        display: grid;
        grid-template-columns: 80px 140px 1fr;
        gap: 0.75rem;
        padding: 0.35rem 0;
        align-items: baseline;
      }}
      .fib-dialog .level-row .pct {{ font-weight: 700; }}
      .fib-dialog .level-row.retracement .pct {{ color: {gold} !important; }}
      .fib-dialog .level-row.extension .pct {{ color: {purple} !important; }}
      .fib-dialog .level-row .name {{ font-weight: 600; }}
      .fib-dialog .level-row .desc {{ font-size: 0.9rem; }}
      .fib-dialog p {{ margin: 0.5rem 0; color: #000000 !important; }}
      .fib-dialog ul {{ padding-left: 1.25rem; margin: 0.5rem 0; }}
      .fib-dialog li {{ margin: 0.4rem 0; }}
      .fib-dialog b, .fib-dialog strong {{ color: #000000 !important; font-weight: 700; }}
      .fib-dialog i, .fib-dialog em {{ color: #000000 !important; }}
    </style>

    <div class="fib-dialog">

      <div class="heading">What Fibonacci levels are</div>
      <p>Fibonacci ratios come from the famous number sequence
        (<b>1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144…</b>) where each
        number is the sum of the two before it. Divide adjacent numbers and
        you get the <b>golden ratio</b> (φ ≈ 1.618), its inverse (0.618),
        and a few related ratios (0.236, 0.382, 0.5, 0.786). These
        proportions show up everywhere in nature, art, and — apparently —
        financial markets. Traders use them as a price framework:
        <b>levels where reversals, pauses, or breakouts often cluster</b>.</p>

      <div class="heading">The math</div>
      <p>Pick a meaningful <b>swing high</b> and <b>swing low</b> on the
        chart — usually the start and end of the most recent significant
        rally. The vertical distance between them is the <b>range</b>:</p>
      <div class="formula">range = swing_high − swing_low</div>
      <p>A <b style="color:{gold} !important">retracement</b> measures how
        much of that rally has been given back. Each level is calculated
        as:</p>
      <div class="formula">retracement_price = swing_high − (range × ratio)</div>
      <p><b>Example.</b> Rally from $50 → $100, so range = $50. The
        <b>50% retracement</b> sits at $100 − ($50 × 0.5) = <b>$75</b>.
        The <b>61.8% retracement</b> sits at $100 − ($50 × 0.618) =
        <b>$69.10</b>.</p>
      <p>An <b style="color:{purple} !important">extension</b> projects
        ABOVE the swing high — used for upside price targets:</p>
      <div class="formula">extension_price = swing_high + (range × ratio)</div>

      <div class="heading">What each level means</div>

      <div class="group-label retracement">Retracements (pullback levels)</div>
      <div class="level-row retracement"><div class="pct">23.6%</div><div class="name">Shallow</div><div class="desc">Stock has barely pulled back. Often the first pause in a strong uptrend; bullish if it holds.</div></div>
      <div class="level-row retracement"><div class="pct">38.2%</div><div class="name">Normal</div><div class="desc">Healthy pullback. Common bounce zone in trending stocks.</div></div>
      <div class="level-row retracement"><div class="pct">50.0%</div><div class="name">Mid</div><div class="desc">Halfway back. Not technically a Fib ratio but watched by everyone, so it acts like one.</div></div>
      <div class="level-row retracement"><div class="pct">61.8%</div><div class="name">Golden</div><div class="desc"><b>The most-watched level.</b> The golden ratio. If price holds here, trend likely intact. If it breaks, expect 78.6% or worse.</div></div>
      <div class="level-row retracement"><div class="pct">78.6%</div><div class="name">Deep</div><div class="desc">Last-chance support. If price breaks here, the prior swing is probably finished.</div></div>

      <div class="group-label extension">Extensions (upside targets)</div>
      <div class="level-row extension"><div class="pct">127.2%</div><div class="name">First target</div><div class="desc">Conservative upside target after a breakout. Where momentum often takes a first pause.</div></div>
      <div class="level-row extension"><div class="pct">161.8%</div><div class="name">Golden target</div><div class="desc"><b>Most common Fib profit target.</b> The golden-ratio extension.</div></div>
      <div class="level-row extension"><div class="pct">261.8%</div><div class="name">Stretch target</div><div class="desc">Reserved for parabolic moves. If price gets here, you've ridden a monster — strongly consider taking profits.</div></div>

      <div class="heading">How traders actually use them</div>
      <ul>
        <li><b>Buying pullbacks in uptrends.</b> If a stock has rallied
          hard, smart money often waits for a pullback to the 38.2%, 50%,
          or 61.8% level before adding. These levels act as support
          because so many other traders are watching them.</li>
        <li><b>Setting stops.</b> A common stop placement is just below
          the next Fib level. Entering at the 50% retracement with a stop
          just under the 61.8% is a classic setup.</li>
        <li><b>Taking profits.</b> The 127.2% and 161.8% extensions are
          popular targets. Many traders scale out — sell ⅓ at 127.2%,
          another ⅓ at 161.8%, let the rest run.</li>
        <li><b>Confluence with other tools.</b> A single Fib level is a
          weak signal. A Fib level that lines up with something else is a
          high-probability decision zone. The patterns to actively hunt:
          <ul style="margin-top:0.35rem;margin-bottom:0.35rem;padding-left:1.1rem">
            <li><b>Fib + moving average</b> &mdash; when the 61.8% retracement
              sits right on the rising 50-day MA (or 38.2% on the 20-day),
              that's two independent supports at the same price. Bounce
              probability jumps materially.</li>
            <li><b>Fib + prior breakout pivot</b> &mdash; if a stock broke
              out at $50, ran to $80, and pulled back to $62, that $62 is
              both the 38.2% retracement AND the prior pivot. Old resistance
              becomes new support; the prior buyers are watching it.</li>
            <li><b>Fib + VCP base low</b> &mdash; the deepest contraction
              in the current Minervini base happens to sit at the 50%
              retracement of the prior swing. The base IS the Fib zone &mdash;
              ideal SEPA buy setup.</li>
            <li><b>Fib + trendline</b> &mdash; a rising trendline drawn
              under recent lows intersects a horizontal Fib level at the
              same price on the same day. Two-axis support from completely
              different methods.</li>
            <li><b>Fib + round number</b> &mdash; $50 / $100 / $200 are
              psychological magnets. A Fib level that sits within 1-2% of
              one of those is reinforced by simple memory: retail and
              institutional both watch round numbers.</li>
          </ul>
          <b>Rule of thumb:</b> if at least two of these line up with a
          Fib level, treat it as a real decision point. If only the Fib
          itself is there, it's a maybe.</li>
        <li><b>The 61.8% is the king.</b> If you only remember one level,
          this is it. The golden ratio, watched by literally every chartist.
          Holds in trending stocks; breaks in failing ones. Use it as
          your trend-still-intact line.</li>
        <li><b>What 23.6% retracement does NOT mean.</b> It does <i>not</i>
          mean the stock dropped 23.6% from the high. It means
          <b>23.6% of the prior rally has been given back</b>. So 23.6% is
          a <i>shallow</i> pullback (line near the top of the range), not
          a deep one. Same logic in reverse — 78.6% means the stock has
          given back nearly all its gains.</li>
      </ul>

      <div class="heading">Honest caveats</div>
      <ul>
        <li><b>Fibs don't predict reversals; they identify candidates.</b>
          Price might bounce off the 61.8%, or it might blast right
          through. Use them as decision zones, not magic numbers.</li>
        <li><b>Swing selection matters.</b> Different chosen swing points
          give different Fib levels. The auto-detector uses the highest
          close in the last ~6 months as the swing high and the lowest
          close before it as the swing low — typically the most useful
          read for swing traders.</li>
        <li><b>Self-fulfilling prophecy.</b> Fibs work partly because so
          many traders watch them and place orders at the same levels.
          They're a coordination tool more than a law of physics.</li>
      </ul>

    </div>
    """
    st.markdown(html, unsafe_allow_html=True)

    if st.button("Close", use_container_width=True, key="fib_explained_close"):
        st.rerun()


def _render_price_tab(d: DeepDiveData):
    # Initialize button-toggle session state for the two Fib toggles
    for _k in (f"fib_retracements_{d.ticker}", f"fib_extensions_{d.ticker}"):
        if _k not in st.session_state:
            st.session_state[_k] = False

    # Three blue buttons: Fib Retracements (toggle), Fib Extensions (toggle),
    # Fibonacci Explained (opens dialog). Matches the same primary-blue
    # button pattern used elsewhere in the app.
    col_fib1, col_fib2, col_explain, col_spacer = st.columns([2, 2, 2, 3])

    r_key = f"fib_retracements_{d.ticker}"
    e_key = f"fib_extensions_{d.ticker}"

    with col_fib1:
        r_label = "✓ FIB RETRACEMENTS" if st.session_state[r_key] else "FIB RETRACEMENTS"
        if st.button(r_label, type="primary", use_container_width=True,
                     key=f"btn_fib_r_{d.ticker}",
                     help="Toggle the 23.6 / 38.2 / 50 / 61.8 / 78.6% pullback levels "
                          "on the chart, auto-detected from the most recent ~6-month swing."):
            st.session_state[r_key] = not st.session_state[r_key]
            st.rerun()

    with col_fib2:
        e_label = "✓ FIB EXTENSIONS" if st.session_state[e_key] else "FIB EXTENSIONS"
        if st.button(e_label, type="primary", use_container_width=True,
                     key=f"btn_fib_e_{d.ticker}",
                     help="Toggle the 127.2 / 161.8 / 261.8% upside projection targets "
                          "on the chart, computed from the same auto-detected swing."):
            st.session_state[e_key] = not st.session_state[e_key]
            st.rerun()

    with col_explain:
        if st.button("📐 FIBONACCI EXPLAINED", type="primary", use_container_width=True,
                     key=f"btn_fib_explain_{d.ticker}",
                     help="Read what Fibonacci levels are, how the math works, and how "
                          "traders typically use them."):
            _fibonacci_explained_dialog()

    show_fib_retracements = st.session_state[r_key]
    show_fib_extensions = st.session_state[e_key]

    # Always compute fib data so it's available for both the chart overlay
    # and the summary table. Cheap — just a few pandas calls.
    try:
        from scanner.setups import fibonacci_levels
        fib_data = fibonacci_levels(d.history)
    except Exception:
        fib_data = None

    fig = _build_price_chart(d)
    if fib_data is not None and (show_fib_retracements or show_fib_extensions):
        _add_fib_lines_to_fig(fig, fib_data, show_fib_retracements, show_fib_extensions)
    # Plotly modebar enabled — gives the user zoom / pan / fullscreen /
    # download right on the inline chart (replaces the standalone Fullscreen
    # button we used to have here).
    st.plotly_chart(fig, use_container_width=True, config={
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"],
    })

    # Summary table below chart if either toggle is on
    if show_fib_retracements or show_fib_extensions:
        _render_fib_summary(d, fib_data, show_fib_retracements, show_fib_extensions)

    # ---- Setup detail panel: pivot + signals row ----
    quality_color = {
        "A": PALETTE["up_green"], "B": PALETTE["accent_cyan"],
        "C": PALETTE["signal_yellow"], "None": PALETTE["text_muted"],
    }.get(d.vcp_quality or "None", PALETTE["text_muted"])
    breakout_html = (
        f"<span style='color:{PALETTE['up_green']};font-weight:700'>● BREAKOUT</span>"
        if d.is_breakout_today else
        f"<span style='color:{PALETTE['text_muted']}'>—</span>"
    )
    powerplay_html = (
        f"<span style='color:{PALETTE['up_green']};font-weight:700'>● POWER PLAY (+{d.power_play_gain:.0f}% in 8wk)</span>"
        if d.is_power_play else
        f"<span style='color:{PALETTE['text_muted']}'>—</span>"
    )
    ext_html = (
        f"<span style='color:{PALETTE['signal_yellow']};font-weight:700'>● EXTENDED (+{d.pct_above_50ma:.1f}% &gt; 50MA)</span>"
        if d.is_extended else
        f"<span style='color:{PALETTE['up_green']}'>{d.pct_above_50ma:+.1f}% from 50MA</span>"
    )
    setup_body = (
        "<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:0.75rem'>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.7rem'>PIVOT</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{PALETTE['accent_cyan']}'>${d.pivot_price:.2f}</div>"
        f"<div style='font-size:0.7rem;color:{PALETTE['text_muted']}'>{d.pivot_date or '—'}</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.7rem'>ATR (20)</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700'>{d.atr_20 if d.atr_20 is not None else '—':.2f}" if d.atr_20 is not None else "—"
        "</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.7rem'>VCP QUALITY</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{quality_color}'>{d.vcp_quality or 'None'}</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.7rem'>VOLUME × AVG</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700'>{d.vol_vs_avg:.1f}x</div></div>"
        "</div>"
        "<div style='display:flex;flex-wrap:wrap;gap:1.5rem;font-family:JetBrains Mono;font-size:0.85rem'>"
        f"<div>{breakout_html}</div>"
        f"<div>{powerplay_html}</div>"
        f"<div>{ext_html}</div>"
        "</div>"
    )
    st.markdown(panel("SETUP DETAIL", setup_body), unsafe_allow_html=True)

    # ---- VCP contraction breakdown (real swing-based detection) ----
    if d.vcp_contractions:
        rows = []
        for c in d.vcp_contractions[-6:]:
            rows.append(
                f"<tr>"
                f"<td>{str(c['peak_date'])[:10]}</td>"
                f"<td style='color:{PALETTE['text_primary']}'>${c['peak_price']:.2f}</td>"
                f"<td>{str(c['trough_date'])[:10]}</td>"
                f"<td style='color:{PALETTE['text_primary']}'>${c['trough_price']:.2f}</td>"
                f"<td style='color:{PALETTE['accent_cyan']};font-weight:600'>−{c['depth_pct']:.1f}%</td>"
                f"</tr>"
            )
        body = (
            "<table style='width:100%;font-family:JetBrains Mono;font-size:0.85rem'>"
            f"<thead><tr style='color:{PALETTE['text_muted']}'>"
            "<th style='text-align:left'>Peak Date</th><th style='text-align:left'>Peak</th>"
            "<th style='text-align:left'>Trough Date</th><th style='text-align:left'>Trough</th>"
            "<th style='text-align:left'>Depth</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
            f"<div style='font-size:0.75rem;color:{PALETTE['text_muted']};margin-top:0.5rem;line-height:1.5'>"
            "Quality VCP shows monotonically <b>decreasing depth</b> across contractions "
            "(e.g., 25% → 15% → 8% → 4%). Each tighter pullback = more supply absorbed by institutions."
            "</div>"
        )
        st.markdown(panel("VCP CONTRACTION BREAKDOWN", body), unsafe_allow_html=True)
    else:
        st.markdown(
            panel(
                "VCP CONTRACTION BREAKDOWN",
                f"<div style='color:{PALETTE['text_muted']}'>No meaningful contractions (≥4%) detected in the last 80 bars.</div>",
            ),
            unsafe_allow_html=True,
        )


def _render_rs_tab(d: DeepDiveData):
    st.plotly_chart(_build_rs_line_chart(d), use_container_width=True, config={"displayModeBar": False})
    explainer = (
        f"<div style='color:{PALETTE['text_muted']};font-size:0.85rem;line-height:1.5'>"
        "The <b>Relative Strength Line</b> is the ratio of this stock's price to SPY. "
        "When it makes a <b>new 50-day high</b> (green triangles), the stock is outperforming the market — "
        "a Minervini/O'Neil leadership signal. RS line at new highs while price is still consolidating "
        "is one of the strongest pre-breakout indications."
        "</div>"
    )
    st.markdown(panel("WHAT THIS MEANS", explainer), unsafe_allow_html=True)


def _fmt_pct(v, dash="—"):
    return f"{v:.1f}%" if v is not None else dash


def _fmt_num(v, fmt="{:.2f}", dash="—"):
    return fmt.format(v) if v is not None else dash


def _fmt_shares(n, dash="—"):
    """Format a share count into a compact M/B string."""
    if n is None:
        return dash
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    return f"{n:,.0f}"


def _metric_html(label: str, value: str, color: str, help_text: str = "") -> str:
    muted = PALETTE['text_muted']
    return (
        f"<div>"
        f"<div style='color:{muted};font-size:0.7rem;text-transform:uppercase;letter-spacing:0.04em'>{label}</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{color}'>{value}</div>"
        + (f"<div style='font-size:0.7rem;color:{muted};margin-top:0.15rem;line-height:1.3'>{help_text}</div>" if help_text else "")
        + "</div>"
    )


def _render_key_ratios_panel(d: DeepDiveData):
    """ROE, Debt/Equity, Forward PE, PEG, Price/Book, Gross Margin —
    color-coded against Minervini/O'Neil thresholds."""
    green = PALETTE['up_green']
    yellow = PALETTE['signal_yellow']
    red = PALETTE['down_red']
    primary = PALETTE['text_primary']

    # ROE color: >=17 green (O'Neil's bar), 10-17 yellow, <10 red
    roe_color = (
        green if (d.roe is not None and d.roe >= 17)
        else yellow if (d.roe is not None and d.roe >= 10)
        else red if (d.roe is not None) else primary
    )
    # Debt/Equity color: <50 green, 50-100 yellow, >100 red (red = highly leveraged)
    de = d.debt_to_equity
    de_color = (
        green if (de is not None and de < 50)
        else yellow if (de is not None and de < 100)
        else red if (de is not None) else primary
    )
    # PEG color: <1 = green (undervalued growth), 1-2 yellow, >2 red
    peg_color = (
        green if (d.peg_ratio is not None and 0 < d.peg_ratio < 1)
        else yellow if (d.peg_ratio is not None and d.peg_ratio < 2)
        else red if (d.peg_ratio is not None and d.peg_ratio >= 2) else primary
    )

    body = (
        "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:0.75rem'>"
        + _metric_html("Return on Equity", _fmt_pct(d.roe), roe_color,
                       "O'Neil/Minervini target: ≥17%. Measures profitability of shareholder capital.")
        + _metric_html("Debt / Equity", _fmt_num(de, "{:.0f}%"), de_color,
                       "Lower is better. >100% = highly leveraged (avoid in tightening conditions).")
        + _metric_html("Gross Margin", _fmt_pct(d.gross_margin), primary,
                       "Higher = pricing power and competitive moat. Stable/rising is bullish.")
        + "</div>"
        "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:1rem'>"
        + _metric_html("Forward P/E", _fmt_num(d.forward_pe), primary,
                       "Price relative to next-year earnings. Context vs sector peers matters more than the absolute number.")
        + _metric_html("PEG Ratio", _fmt_num(d.peg_ratio), peg_color,
                       "PE divided by growth rate. <1 = growth is undervalued. >2 = expensive growth.")
        + _metric_html("Price / Book", _fmt_num(d.price_to_book), primary,
                       "Price vs accounting book value. Mostly useful for financials and asset-heavy names.")
        + "</div>"
    )
    st.markdown(panel("KEY RATIOS", body), unsafe_allow_html=True)


def _render_ownership_panel(d: DeepDiveData):
    """Institutional %, Insider %, Float size, Short interest —
    O'Neil-style accumulation/distribution context."""
    green = PALETTE['up_green']
    yellow = PALETTE['signal_yellow']
    red = PALETTE['down_red']
    primary = PALETTE['text_primary']
    cyan = PALETTE['accent_cyan']

    # Institutional %: 30-80% sweet spot (room for more accumulation), <30 too thin, >85 too crowded
    inst = d.institutional_pct
    inst_color = (
        green if (inst is not None and 30 <= inst <= 80)
        else yellow if (inst is not None) else primary
    )
    # Insider %: >10% = strong insider alignment (bullish)
    insd = d.insider_pct
    insd_color = (
        green if (insd is not None and insd >= 10)
        else primary
    )
    # Short % of float: >15% = high short interest (squeeze potential AND skeptical thesis)
    sp = d.short_pct_of_float
    sp_color = (
        cyan if (sp is not None and sp >= 15)
        else primary
    )
    # Float size context: <50M = small float (high beta/volatility); 50-300M mid; 300M+ large
    fs = d.float_shares
    fs_color = (
        green if (fs is not None and fs < 50_000_000)
        else primary
    )

    body = (
        "<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:0.75rem'>"
        + _metric_html("Institutional %", _fmt_pct(inst), inst_color,
                       "O'Neil's 'I' in CAN SLIM. 30-80% = room to accumulate. >85% = crowded.")
        + _metric_html("Insider %", _fmt_pct(insd), insd_color,
                       "Insider ownership. >10% = strong alignment with shareholders.")
        + _metric_html("Float", _fmt_shares(fs), fs_color,
                       "Shares actually available to trade. <50M = small float = higher volatility, easier momentum.")
        + _metric_html("Shares Out", _fmt_shares(d.shares_outstanding), primary,
                       "Total shares outstanding. Float = shares-out minus insider/restricted lockup.")
        + "</div>"
        "<div style='display:grid;grid-template-columns:repeat(2,1fr);gap:1rem'>"
        + _metric_html("Short % of Float", _fmt_pct(sp), sp_color,
                       "% of float sold short. >15% = elevated. High short + breakout = squeeze potential.")
        + _metric_html("Short Ratio (days to cover)", _fmt_num(d.short_ratio, "{:.1f}"), primary,
                       "Days of avg volume to cover all shorts. Higher = harder to exit short positions.")
        + "</div>"
    )
    st.markdown(panel("OWNERSHIP & SHORT INTEREST", body), unsafe_allow_html=True)


def _render_cache_age_captions(ticker: str):
    """Small white captions showing how fresh each cached field group is.

    User wants the cache state visible at all times so they always know
    whether they\'re looking at fresh or stale data. Renders only the
    groups that actually have a cached_at timestamp; silently skips any
    that are missing (which means they were fetched live this scan).
    """
    try:
        from scanner.data_cache import get_cached_fundamentals, cache_age_label
    except Exception:
        return
    try:
        entry = get_cached_fundamentals(ticker)
    except Exception:
        return
    if not entry:
        return

    parts: list[str] = []
    label_static = cache_age_label(entry.get("static_cached_at"))
    label_mktcap = cache_age_label(entry.get("mktcap_cached_at"))
    label_growth = cache_age_label(entry.get("growth_cached_at"))
    if label_static:
        parts.append(f"Sector/Industry: {label_static}")
    if label_mktcap:
        parts.append(f"Mkt Cap: {label_mktcap}")
    if label_growth:
        parts.append(f"PEG/EPS/ER: {label_growth}")
    if not parts:
        return

    # Tiny white mono caption. Subtle but always readable.
    text = "  &middot;  ".join(parts)
    html = (
        "<div style=\"font-family:'JetBrains Mono',monospace;font-size:0.72rem;"
        "color:#ffffff;opacity:0.75;margin-top:0.25rem;margin-bottom:0.6rem\">"
        f"DATA FRESHNESS &middot; {text}"
        "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def _render_fundamentals_tab(d: DeepDiveData):
    # Cache-freshness banner at the very top of the tab so the user can
    # always tell what\'s live vs cached.
    _render_cache_age_captions(d.ticker)

    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(_build_eps_chart(d), use_container_width=True, config={"displayModeBar": False})
    with col2:
        st.plotly_chart(_build_revenue_chart(d), use_container_width=True, config={"displayModeBar": False})

    # Margins panel
    pm = f"{d.profit_margin:.2f}%" if d.profit_margin is not None else "N/A"
    om = f"{d.operating_margin:.2f}%" if d.operating_margin is not None else "N/A"
    body = (
        "<div style='display:grid;grid-template-columns:1fr 1fr;gap:1rem'>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>PROFIT MARGIN</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{PALETTE['up_green'] if (d.profit_margin or 0) > 0 else PALETTE['down_red']}'>{pm}</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>OPERATING MARGIN</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{PALETTE['up_green'] if (d.operating_margin or 0) > 0 else PALETTE['down_red']}'>{om}</div></div>"
        "</div>"
    )
    st.markdown(panel("MARGINS (TTM)", body), unsafe_allow_html=True)

    # ---- Key Ratios panel ----
    _render_key_ratios_panel(d)

    # ---- Ownership panel ----
    _render_ownership_panel(d)


def _render_execution_tab(d: DeepDiveData):
    risk_color = PALETTE["up_green"] if d.risk_pct <= 7 else (PALETTE["signal_yellow"] if d.risk_pct <= 10 else PALETTE["down_red"])

    # Suggested trade levels (no shares / no position value)
    ticket_html = (
        "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem'>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>SUGGESTED BUY STOP</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700;color:{PALETTE['up_green']}'>${d.buy_trigger:.2f}</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>SUGGESTED HARD STOP</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700;color:{PALETTE['down_red']}'>${d.natural_stop:.2f}</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>RISK / SHARE</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700'>${d.risk_per_share:.2f} <span style='color:{risk_color};font-size:1rem'>({d.risk_pct:.1f}%)</span></div></div>"
        "</div>"
    )
    st.markdown(panel("SUGGESTED TRADE LEVELS", ticket_html), unsafe_allow_html=True)

    # ---- Position calculator (user input) ----
    st.markdown(
        f"<div class='panel-title'>POSITION CALCULATOR</div>"
        f"<div style='color:{PALETTE['text_muted']};font-size:0.8rem;margin-bottom:0.5rem'>"
        "Enter your actual entry price and share count to see live cost basis, max loss at suggested stop, and dollar gains at each target."
        "</div>",
        unsafe_allow_html=True,
    )
    pc1, pc2 = st.columns(2)
    with pc1:
        price_paid = st.number_input(
            "Price Paid ($)",
            min_value=0.0, value=float(d.buy_trigger), step=0.01,
            key=f"price_paid_{d.ticker}",
            format="%.2f",
        )
    with pc2:
        shares_owned = st.number_input(
            "Shares Owned",
            min_value=0, value=0, step=1,
            key=f"shares_owned_{d.ticker}",
        )

    cost_basis = price_paid * shares_owned
    max_loss = (price_paid - d.natural_stop) * shares_owned if shares_owned > 0 else 0
    max_loss_pct = ((price_paid - d.natural_stop) / price_paid * 100) if price_paid > 0 else 0

    if shares_owned > 0:
        loss_color = PALETTE["down_red"] if max_loss > 0 else PALETTE["text_muted"]
        calc_body = (
            "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem'>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>COST BASIS</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{PALETTE['accent_cyan']}'>${cost_basis:,.2f}</div></div>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>MAX LOSS AT STOP (${d.natural_stop:.2f})</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{loss_color}'>-${max_loss:,.2f} <span style='font-size:0.9rem'>({max_loss_pct:.1f}%)</span></div></div>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>R-VALUE</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700'>${(price_paid - d.natural_stop):.2f}</div></div>"
            "</div>"
        )
        st.markdown(panel("YOUR POSITION", calc_body), unsafe_allow_html=True)

    # ---- Stop reasoning panel ----
    if d.stop_candidates:
        rows = []
        for label, val in d.stop_candidates.items():
            is_chosen = (label == d.stop_reason)
            color = PALETTE["accent_cyan"] if is_chosen else PALETTE["text_muted"]
            indicator = "✓ chosen" if is_chosen else ""
            rows.append(
                f"<tr style='color:{color}'>"
                f"<td>{label}</td>"
                f"<td style='font-family:JetBrains Mono'>${val:.2f}</td>"
                f"<td style='font-style:italic'>{indicator}</td>"
                f"</tr>"
            )
        body = (
            "<table style='width:100%;font-family:JetBrains Mono;font-size:0.85rem'>"
            f"<thead><tr style='color:{PALETTE['text_muted']}'>"
            "<th style='text-align:left'>Stop Candidate</th><th style='text-align:left'>Price</th><th></th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
            f"<div style='font-size:0.75rem;color:{PALETTE['text_muted']};margin-top:0.5rem;line-height:1.5'>"
            "We pick the <b>highest valid stop</b> (= tightest acceptable risk) that's still inside Minervini's "
            "7% hard ceiling. Tighter stops = more shares for the same dollar risk."
            "</div>"
        )
        st.markdown(panel("STOP REASONING", body), unsafe_allow_html=True)

    # ---- Profit targets ----
    # Use user-entered price_paid if provided, else show suggested-trade math.
    base_price = price_paid if price_paid > 0 else d.buy_trigger
    R = base_price - d.natural_stop  # 1R = risk per share from user's entry
    if R <= 0:
        R = d.risk_per_share

    show_dollars = shares_owned > 0
    targets_html = (
        "<table style='width:100%;font-family:JetBrains Mono;font-size:0.9rem'>"
        f"<thead><tr style='color:{PALETTE['text_muted']};text-align:left'>"
        "<th>Target</th><th>Price</th>"
        + ("<th>Gain $ (your shares)</th>" if show_dollars else "<th>Gain per share</th>")
        + "<th>R-Multiple / %</th></tr></thead><tbody>"
    )
    targets = [
        ("1R",   base_price + R),
        ("2R",   base_price + 2 * R),
        ("3R",   base_price + 3 * R),
        ("+5%",  base_price * 1.05),
        ("+10%", base_price * 1.10),
        ("+20%", base_price * 1.20),
    ]
    for label, price in targets:
        gain_per_share = price - base_price
        gain_dollars = gain_per_share * (shares_owned if show_dollars else 1)
        r_mult = (price - base_price) / R if R > 0 else 0
        pct = (price - base_price) / base_price * 100 if base_price > 0 else 0
        if show_dollars:
            gain_str = f"+${gain_dollars:,.2f}"
        else:
            gain_str = f"+${gain_per_share:.2f}"
        green = PALETTE["up_green"]
        muted = PALETTE["text_muted"]
        targets_html += (
            f"<tr><td>{label}</td><td>${price:.2f}</td>"
            f"<td style='color:{green}'>{gain_str}</td>"
            f"<td>{r_mult:.1f}R &nbsp; <span style='color:{muted}'>(+{pct:.1f}%)</span></td></tr>"
        )
    targets_html += "</tbody></table>"

    if show_dollars:
        target_caption = f"Computed from your entry of <b>${price_paid:.2f}</b> x <b>{shares_owned:,}</b> shares."
    else:
        target_caption = "Enter Price Paid + Shares Owned above to see dollar gains."
    muted = PALETTE["text_muted"]
    targets_html += f"<div style='font-size:0.75rem;color:{muted};margin-top:0.5rem'>{target_caption}</div>"

    st.markdown(panel("PROFIT TARGETS", targets_html), unsafe_allow_html=True)

    # Sector context
    sector_html = (
        f"<div><b>Sector:</b> {d.sector}</div>"
        f"<div><b>Industry:</b> {d.industry}</div>"
        f"<div style='color:{PALETTE['text_muted']};font-size:0.8rem;margin-top:0.5rem'>"
        "Group leadership matters: Minervini emphasizes that the strongest stocks tend to come from the strongest industries. "
        "Cross-check this stock's industry RS against other leaders before sizing in."
        "</div>"
    )
    st.markdown(panel("SECTOR / INDUSTRY CONTEXT", sector_html), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def _render_short_execution_tab(d: DeepDiveData):
    """Short-side execution panel.

    Computes short-trade levels from the deep-dive history (we don't store
    these on the DeepDiveData since fetch_deep_dive is shared across modes).
      - SHORT BELOW = today's low * 0.995 (sell-stop entry on break of low)
      - COVER STOP  = max(today's high, last 5 days' high) * 1.01
      - Targets: 20-day MA, 50-day MA, -5%, -10%, -20%
    Plus a position calculator with Price Shorted + Shares Shorted that
    computes cost basis, max loss at cover stop, and dollar profit at each
    downside target.
    """
    h = d.history
    today_low = float(h["Low"].iloc[-1])
    today_high = float(h["High"].iloc[-1])
    recent_5d_high = float(h["High"].iloc[-5:].max())
    sma_20 = float(h["Close"].rolling(20).mean().iloc[-1])
    sma_50 = float(h["SMA50"].iloc[-1]) if "SMA50" in h.columns else sma_20

    short_below = round(today_low * 0.995, 2)
    cover_stop = round(max(today_high, recent_5d_high) * 1.01, 2)
    risk_per_share = cover_stop - short_below
    risk_pct = (risk_per_share / short_below * 100) if short_below > 0 else 0
    target_20ma = round(sma_20, 2)
    target_50ma = round(sma_50, 2)

    risk_color = (
        PALETTE["up_green"] if risk_pct <= 7
        else (PALETTE["signal_yellow"] if risk_pct <= 10 else PALETTE["down_red"])
    )

    # ---- Suggested levels ticket ----
    ticket_html = (
        "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem'>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>SUGGESTED SHORT BELOW</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700;color:{PALETTE['down_red']}'>${short_below:.2f}</div>"
        f"<div style='font-size:0.7rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>Sell-stop entry: short when price breaks today low</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>COVER STOP</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700;color:{PALETTE['up_green']}'>${cover_stop:.2f}</div>"
        f"<div style='font-size:0.7rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>Buy-to-cover above today/recent high</div></div>"
        f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>RISK / SHARE</div>"
        f"<div style='font-family:JetBrains Mono;font-size:1.6rem;font-weight:700'>${risk_per_share:.2f} <span style='color:{risk_color};font-size:1rem'>({risk_pct:.1f}%)</span></div>"
        f"<div style='font-size:0.7rem;color:{PALETTE['text_muted']};margin-top:0.2rem'>If you cover at the stop</div></div>"
        "</div>"
    )
    st.markdown(panel("SUGGESTED SHORT LEVELS", ticket_html), unsafe_allow_html=True)

    # ---- Position calculator (user input) ----
    st.markdown(
        f"<div class='panel-title'>POSITION CALCULATOR</div>"
        f"<div style='color:{PALETTE['text_muted']};font-size:0.8rem;margin-bottom:0.5rem'>"
        "Enter your actual short entry price and share count to see live cost basis, "
        "max loss at the suggested cover stop, and dollar profit at each downside target."
        "</div>",
        unsafe_allow_html=True,
    )
    pc1, pc2 = st.columns(2)
    with pc1:
        price_shorted = st.number_input(
            "Price Shorted ($)",
            min_value=0.0, value=float(short_below), step=0.01,
            key=f"short_price_{d.ticker}", format="%.2f",
        )
    with pc2:
        shares_shorted = st.number_input(
            "Shares Shorted",
            min_value=0, value=0, step=1,
            key=f"short_shares_{d.ticker}",
        )

    cost_basis = price_shorted * shares_shorted
    max_loss = (cover_stop - price_shorted) * shares_shorted if shares_shorted > 0 else 0
    max_loss_pct = ((cover_stop - price_shorted) / price_shorted * 100) if price_shorted > 0 else 0

    if shares_shorted > 0:
        loss_color = PALETTE["down_red"] if max_loss > 0 else PALETTE["text_muted"]
        calc_body = (
            "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem'>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>POSITION VALUE</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{PALETTE['accent_cyan']}'>${cost_basis:,.2f}</div></div>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>MAX LOSS AT COVER (${cover_stop:.2f})</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700;color:{loss_color}'>-${max_loss:,.2f} <span style='font-size:0.9rem'>({max_loss_pct:.1f}%)</span></div></div>"
            f"<div><div style='color:{PALETTE['text_muted']};font-size:0.75rem'>R-VALUE</div>"
            f"<div style='font-family:JetBrains Mono;font-size:1.4rem;font-weight:700'>${(cover_stop - price_shorted):.2f}</div></div>"
            "</div>"
        )
        st.markdown(panel("YOUR SHORT POSITION", calc_body), unsafe_allow_html=True)

    # ---- Profit targets (price drops) ----
    base_price = price_shorted if price_shorted > 0 else short_below
    R = cover_stop - base_price
    if R <= 0:
        R = max(risk_per_share, 0.01)

    show_dollars = shares_shorted > 0
    targets_html = (
        "<table style='width:100%;font-family:JetBrains Mono;font-size:0.9rem'>"
        f"<thead><tr style='color:{PALETTE['text_muted']};text-align:left'>"
        "<th>Target</th><th>Price</th>"
        + ("<th>Gain $ (your shares)</th>" if show_dollars else "<th>Gain per share</th>")
        + "<th>R-Multiple / %</th></tr></thead><tbody>"
    )
    targets = [
        ("1R",        base_price - R),
        ("2R",        base_price - 2 * R),
        ("3R",        base_price - 3 * R),
        ("20-day MA", target_20ma),
        ("50-day MA", target_50ma),
        ("-5%",       base_price * 0.95),
        ("-10%",      base_price * 0.90),
        ("-20%",      base_price * 0.80),
    ]
    green = PALETTE["up_green"]
    muted = PALETTE["text_muted"]
    for label, price in targets:
        gain_per_share = base_price - price
        gain_dollars = gain_per_share * (shares_shorted if show_dollars else 1)
        r_mult = (base_price - price) / R if R > 0 else 0
        pct = (base_price - price) / base_price * 100 if base_price > 0 else 0
        if show_dollars:
            gain_str = f"+${gain_dollars:,.2f}"
        else:
            gain_str = f"+${gain_per_share:.2f}"
        targets_html += (
            f"<tr><td>{label}</td><td>${price:.2f}</td>"
            f"<td style='color:{green}'>{gain_str}</td>"
            f"<td>{r_mult:.1f}R &nbsp; <span style='color:{muted}'>(-{pct:.1f}%)</span></td></tr>"
        )
    targets_html += "</tbody></table>"
    if show_dollars:
        target_caption = f"Computed from your short entry of <b>${price_shorted:.2f}</b> x <b>{shares_shorted:,}</b> shares. Profit = price drops to that target."
    else:
        target_caption = "Enter Price Shorted + Shares Shorted above to see dollar gains. Profit on a short = price falls below your entry."
    targets_html += f"<div style='font-size:0.75rem;color:{muted};margin-top:0.5rem'>{target_caption}</div>"
    st.markdown(panel("PROFIT TARGETS (price drops)", targets_html), unsafe_allow_html=True)

    sector_html = (
        f"<div><b>Sector:</b> {d.sector}</div>"
        f"<div><b>Industry:</b> {d.industry}</div>"
        f"<div style='color:{PALETTE['text_muted']};font-size:0.8rem;margin-top:0.5rem'>"
        "For shorts, prefer non-leadership names — leaders in top-performing industries "
        "tend to mean-revert less reliably. The Industry Rank shown in the candidates "
        "table is a useful filter."
        "</div>"
    )
    st.markdown(panel("SECTOR / INDUSTRY CONTEXT", sector_html), unsafe_allow_html=True)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_deep_dive(ticker: str, account_size: float, risk_per_trade: float):
    return fetch_deep_dive(ticker, account_size, risk_per_trade)


def _render_social_tab(d):
    """SOCIAL deep-dive tab — StockTwits stream, news, sentiment+mention trend.

    Lazy-loads everything when the tab is opened so it never adds time to the
    main scan or to any other deep-dive tab. If StockTwits or news APIs are
    down, surfaces a friendly error and continues — never blocks the tab.
    """
    import streamlit as st
    import pandas as pd
    import plotly.graph_objects as go

    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    primary = PALETTE["text_primary"]
    up = PALETTE["up_green"]
    down = PALETTE["down_red"]
    yellow = PALETTE["signal_yellow"]

    ticker = d.ticker

    # ---- Reddit posts (replaces blocked StockTwits stream) ----
    st.markdown(
        f"<h4 style='color:{cyan};margin-top:0'>🟠 Reddit Mentions</h4>"
        f"<div style='color:{muted};font-size:0.8rem;margin-bottom:0.75rem'>"
        f"Recent posts mentioning ${ticker} across r/wallstreetbets, r/stocks, r/investing, "
        f"r/pennystocks, r/options, r/smallstreetbets, r/StockMarket (last 24h).</div>",
        unsafe_allow_html=True,
    )

    try:
        with st.spinner("Loading Reddit posts..."):
            reddit_data = _cached_reddit_mentions(ticker, 15, "day")
    except Exception as e:
        reddit_data = {"posts": [], "total_count": 0, "subreddit_counts": {}, "error": str(e)}

    if reddit_data.get("error"):
        st.warning(f"Reddit unavailable: {reddit_data['error']}")
    else:
        posts = reddit_data.get("posts", [])
        sub_counts = reddit_data.get("subreddit_counts", {})
        total = reddit_data.get("total_count", 0)

        # Subreddit distribution bar (cyan-shades horizontal stack)
        if total > 0:
            sub_summary_parts = []
            for sub, n in sub_counts.items():
                if n > 0:
                    sub_summary_parts.append(
                        f"<b style='color:{cyan}'>r/{sub}</b>: {n}"
                    )
            sub_summary = " &middot; ".join(sub_summary_parts) if sub_summary_parts else "—"
            st.markdown(
                f"<div style='font-family:JetBrains Mono;font-size:0.8rem;color:{muted};"
                f"margin-bottom:0.8rem'>"
                f"<b style='color:{primary}'>{total}</b> posts found &middot; {sub_summary}"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption(
                f"No recent posts mentioning ${ticker} on the tracked subreddits in the last 24h. "
                f"Try widening the time filter (currently 'day') if you want longer windows — "
                f"that requires a small code change."
            )

        # Post list — show top 10 by score
        if posts:
            from datetime import datetime as _dt_local
            sorted_posts = sorted(posts, key=lambda p: p.get("score", 0), reverse=True)[:10]
            st.markdown(
                f"<div style='font-family:JetBrains Mono;font-size:0.8rem;color:{muted};"
                f"margin-bottom:0.4rem'>TOP 10 POSTS BY UPVOTES</div>",
                unsafe_allow_html=True,
            )
            for p in sorted_posts:
                score = p.get("score", 0)
                n_comments = p.get("num_comments", 0)
                sub = p.get("subreddit", "")
                author = p.get("author", "anon")
                title = (p.get("title", "") or "").replace("<", "&lt;").replace(">", "&gt;")
                permalink = p.get("permalink", "") or ""
                body = (p.get("body", "") or "").replace("<", "&lt;").replace(">", "&gt;")
                flair = p.get("flair", "") or ""
                created_ts = p.get("created_utc")
                created_str = ""
                if created_ts:
                    try:
                        created_str = _dt_local.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass

                # Color the score: orange-ish for any positive, gray for 0/negative
                score_color = "#ff7a45" if score > 50 else (cyan if score > 0 else muted)
                flair_html = (
                    f" &middot; <span style='color:{yellow}'>{flair}</span>" if flair else ""
                )
                # Truncate body to ~3 lines worth
                if len(body) > 400:
                    body = body[:400] + "..."
                body_html = (
                    f"<div style='font-family:Inter;font-size:0.82rem;color:{primary};"
                    f"line-height:1.45;margin-top:0.35rem;opacity:0.9'>{body}</div>"
                    if body.strip() else ""
                )
                title_html = (
                    f"<a href='{permalink}' target='_blank' style='color:{primary};"
                    f"text-decoration:none;font-weight:600'>{title}</a>"
                    if permalink else title
                )

                st.markdown(
                    f"<div style='border-left:3px solid {score_color};padding:0.5rem 0.75rem;"
                    f"margin-bottom:0.5rem;background:{PALETTE['bg_panel']};border-radius:4px'>"
                    f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
                    f"margin-bottom:0.3rem'>"
                    f"<b style='color:{score_color}'>▲ {score}</b> &middot; "
                    f"💬 {n_comments} &middot; "
                    f"<b style='color:{cyan}'>r/{sub}</b> &middot; "
                    f"u/{author} &middot; {created_str}"
                    f"{flair_html}"
                    f"</div>"
                    f"<div style='font-family:Inter;font-size:0.95rem;color:{primary};"
                    f"line-height:1.4'>{title_html}</div>"
                    f"{body_html}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    # ---- News headlines (yfinance .news, lazy) ----
    st.markdown(
        f"<h4 style='color:{cyan};margin-top:1.5rem'>📰 Recent News</h4>"
        f"<div style='color:{muted};font-size:0.8rem;margin-bottom:0.75rem'>"
        f"Latest headlines from Yahoo Finance for {ticker}.</div>",
        unsafe_allow_html=True,
    )
    try:
        import yfinance as yf
        from datetime import datetime as _dt
        with st.spinner("Loading news..."):
            news_items = yf.Ticker(ticker).news or []
        if not news_items:
            st.caption("No news items returned.")
        else:
            # yfinance returns a list of dicts with title, publisher, link,
            # providerPublishTime (unix timestamp). Newer yfinance versions
            # wrap these inside .content. Handle both shapes defensively.
            shown = 0
            for item in news_items[:10]:
                # Newer yfinance: dict with 'content' subkey
                if isinstance(item, dict) and "content" in item:
                    content = item.get("content") or {}
                    title = content.get("title") or ""
                    publisher = (content.get("provider") or {}).get("displayName", "")
                    link = (content.get("canonicalUrl") or {}).get("url", "")
                    pub_time = content.get("pubDate", "")
                else:
                    title = item.get("title", "") if isinstance(item, dict) else ""
                    publisher = item.get("publisher", "") if isinstance(item, dict) else ""
                    link = item.get("link", "") if isinstance(item, dict) else ""
                    pub_ts = item.get("providerPublishTime") if isinstance(item, dict) else None
                    pub_time = ""
                    if pub_ts:
                        try:
                            pub_time = _dt.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            pass
                if not title:
                    continue
                safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
                link_html = (
                    f"<a href='{link}' target='_blank' style='color:{cyan};text-decoration:none'>"
                    f"{safe_title}</a>" if link else safe_title
                )
                st.markdown(
                    f"<div style='padding:0.5rem 0.75rem;margin-bottom:0.4rem;"
                    f"background:{PALETTE['bg_panel']};border-radius:4px;"
                    f"border-left:3px solid {cyan}'>"
                    f"<div style='font-family:Inter;font-size:0.9rem;font-weight:600;"
                    f"line-height:1.35'>{link_html}</div>"
                    f"<div style='font-family:JetBrains Mono;font-size:0.7rem;color:{muted};"
                    f"margin-top:0.25rem'>{publisher} &middot; {pub_time}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                shown += 1
            if shown == 0:
                st.caption("No usable news items.")
    except Exception as e:
        st.warning(f"News unavailable: {e}")

    # ---- Mention + sentiment trend chart (from snapshot history) ----
    st.markdown(
        f"<h4 style='color:{cyan};margin-top:1.5rem'>📊 Mention + Sentiment Trend</h4>"
        f"<div style='color:{muted};font-size:0.8rem;margin-bottom:0.75rem'>"
        f"Daily mentions (bars) and sentiment % (line) from your social-scan snapshot history. "
        f"Populates as you accumulate refreshes.</div>",
        unsafe_allow_html=True,
    )
    try:
        from scanner.social_scan import load_social_snapshots
        snaps = load_social_snapshots(days_back=90)
        # Pull this ticker's data point from each snapshot
        timeline = []
        for snap in snaps:
            ts = snap.get("timestamp")
            for cand in snap.get("candidates", []):
                if (cand.get("Ticker") or "").upper() == ticker.upper():
                    timeline.append({
                        "ts": ts,
                        "mentions": cand.get("Mentions"),
                        "sentiment": cand.get("Sentiment_Score"),
                    })
                    break

        if len(timeline) < 2:
            st.caption(
                f"Need at least 2 snapshots with {ticker} present. "
                f"Currently have {len(timeline)} — keep refreshing the Twitter scan over time."
            )
        else:
            df_tl = pd.DataFrame(timeline)
            df_tl["ts"] = pd.to_datetime(df_tl["ts"])
            df_tl = df_tl.sort_values("ts")

            fig = go.Figure()
            # Mentions on left axis (bar)
            fig.add_trace(go.Bar(
                x=df_tl["ts"], y=df_tl["mentions"],
                name="Mentions",
                marker_color=cyan,
                opacity=0.55,
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Mentions: %{y}<extra></extra>",
            ))
            # Sentiment on right axis (line, 0-100)
            fig.add_trace(go.Scatter(
                x=df_tl["ts"], y=df_tl["sentiment"],
                name="Sentiment %",
                yaxis="y2",
                line=dict(color=up, width=2),
                marker=dict(size=7),
                mode="lines+markers",
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Sentiment: %{y:.0f}%<extra></extra>",
            ))
            fig.update_layout(
                template="plotly_dark",
                dragmode="pan",
                plot_bgcolor=PALETTE["bg_main"],
                paper_bgcolor=PALETTE["bg_main"],
                font=dict(family="JetBrains Mono, monospace", color=primary, size=11),
                margin=dict(l=50, r=60, t=30, b=40),
                height=350,
                xaxis=dict(gridcolor=PALETTE["border"], title=""),
                yaxis=dict(gridcolor=PALETTE["border"], title="Mentions"),
                yaxis2=dict(
                    gridcolor=PALETTE["border"], title="Sentiment %",
                    overlaying="y", side="right", range=[0, 100],
                ),
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, font=dict(color="#ffffff"),
                ),
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    except Exception as e:
        st.caption(f"Trend chart unavailable: {e}")


def render_deep_dive(ticker: str, account_size: float, risk_per_trade: float):
    """Render the full deep-dive page for one ticker.

    Tab order intentionally puts EXECUTION first. The execution tab content
    depends on which mode opened the deep-dive:
      - LONGS  -> long-side execution
      - SHORTS -> short-side execution
    """
    import streamlit.components.v1 as components
    scroll_js = """
    <script>
    function _lbScrollTop() {
        try {
            var doc = window.parent.document;
            var selectors = [
                'section.main',
                '[data-testid="stMain"]',
                '[data-testid="stAppViewContainer"]',
                '[data-testid="stMainBlockContainer"]',
                '.main',
                '.block-container'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = doc.querySelector(selectors[i]);
                if (el) { el.scrollTop = 0; el.scrollLeft = 0; }
            }
            doc.documentElement.scrollTop = 0;
            doc.body.scrollTop = 0;
            window.parent.scrollTo(0, 0);
        } catch(e) { try { window.parent.scrollTo(0, 0); } catch(_) {} }
    }
    _lbScrollTop();
    setTimeout(_lbScrollTop, 50);
    setTimeout(_lbScrollTop, 150);
    setTimeout(_lbScrollTop, 400);
    setTimeout(_lbScrollTop, 800);
    setTimeout(_lbScrollTop, 1500);
    </script>
    """
    components.html(scroll_js, height=0)

    with st.spinner(f"Loading deep dive for {ticker}..."):
        d = _cached_deep_dive(ticker, account_size, risk_per_trade)
    if d is None:
        st.error(f"Could not load data for {ticker}. The ticker may have been delisted or Yahoo Finance is unavailable.")
        return

    _render_header(d)

    is_short_mode = st.session_state.get("mode") == "shorts"
    exec_label = "🎯 SHORT EXECUTION" if is_short_mode else "🎯 EXECUTION"

    tab_fund, tab_social, tab_exec, tab_price, tab_rs = st.tabs(
        ["💰 FUNDAMENTALS", "🐦 SOCIAL", exec_label, "📈 CHART", "📊 RS LINE"]
    )
    with tab_fund:
        _render_fundamentals_tab(d)
    with tab_social:
        _render_social_tab(d)
    with tab_exec:
        if is_short_mode:
            _render_short_execution_tab(d)
        else:
            _render_execution_tab(d)
    with tab_price:
        _render_price_tab(d)
    with tab_rs:
        _render_rs_tab(d)
