"""
Dark Bloomberg/TradingView-style theme for Streamlit.

Inject this once at the top of every page via `apply_theme()`.
Color palette:
  - bg_main:      #0a0e17  (near black, slight blue)
  - bg_panel:     #131826  (raised surface)
  - bg_panel_2:   #1b2233  (table rows)
  - border:       #2a3447
  - text_primary: #e8eaf0  (off-white)
  - text_muted:   #7a8294  (gray)
  - accent_cyan:  #00d4ff  (links, highlights)
  - up_green:     #26d97f  (gains, positive)
  - down_red:     #ff4d6d  (losses, negative)
  - signal_yellow:#ffb547
  - signal_red:   #ff4d6d
  - signal_green: #26d97f
"""

import streamlit as st


# Palette also exported for use in plotly charts
PALETTE = {
    "bg_main":       "#0a0e17",
    "bg_panel":      "#131826",
    "bg_panel_2":    "#1b2233",
    "border":        "#2a3447",
    "text_primary":  "#e8eaf0",
    "text_muted":    "#7a8294",
    "accent_cyan":   "#00d4ff",
    "up_green":      "#26d97f",
    "down_red":      "#ff4d6d",
    "signal_yellow": "#ffb547",
    "signal_red":    "#ff4d6d",
    "signal_green":  "#26d97f",
    "vcp_zone":      "rgba(0, 212, 255, 0.12)",
}


_CSS = f"""
<style>
/* ---- Base ---- */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap');

/* Bump the root font size from the browser default of 16px to 17px. This
   makes every rem-sized text in the app ~6% bigger without changing layout.
   Modest, easier on the eyes, not so big that it breaks any tight columns. */
html {{
    font-size: 17px !important;
}}

html, body, [data-testid="stAppViewContainer"], .stApp {{
    background-color: {PALETTE['bg_main']} !important;
    color: {PALETTE['text_primary']} !important;
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
}}

/* Hide Streamlit's default chrome we don't need */
#MainMenu {{visibility: hidden;}}
footer {{visibility: hidden;}}

/* ----------------------------------------------------------------------
   Streamlit's top-right header strip: blend the bar into the page
   background; force EVERYTHING inside (Deploy button, hamburger menu,
   running indicator, theme toggle) to render as white icons with no
   surrounding pill. Multiple selectors below catch every Streamlit
   version's class naming variations.
   ---------------------------------------------------------------------- */
header[data-testid="stHeader"],
[data-testid="stHeader"],
.stApp > header,
header.stAppHeader {{
    background-color: transparent !important;
    background: transparent !important;
    border-bottom: none !important;
    box-shadow: none !important;
}}
/* All toolbar containers / status widgets / decorative bars — transparent */
[data-testid="stToolbar"],
[data-testid="stToolbarActions"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stHeaderActionElements"],
[data-testid="stHeader"] > div,
[data-testid="stHeader"] > div > div {{
    background-color: transparent !important;
    background: transparent !important;
    box-shadow: none !important;
}}
/* Deploy button — needs explicit hits because Streamlit gives it
   its own dedicated test-id and a forced white background. */
[data-testid="stDeployButton"],
[data-testid="stHeaderDeployButton"],
.stDeployButton,
.stDeployButton button,
button[title="Deploy this app"] {{
    background-color: transparent !important;
    background: transparent !important;
    color: #ffffff !important;
    border: 1px solid rgba(255, 255, 255, 0.25) !important;
    border-radius: 4px !important;
    box-shadow: none !important;
}}
[data-testid="stDeployButton"]:hover,
[data-testid="stHeaderDeployButton"]:hover,
.stDeployButton:hover,
.stDeployButton button:hover {{
    background-color: rgba(255, 255, 255, 0.08) !important;
    border-color: {PALETTE['accent_cyan']} !important;
    color: {PALETTE['accent_cyan']} !important;
}}
/* All buttons / SVGs in the header become white text + white icons */
header[data-testid="stHeader"] button,
[data-testid="stToolbar"] button,
[data-testid="stHeaderActionElements"] button {{
    background-color: transparent !important;
    color: #ffffff !important;
    border: none !important;
    box-shadow: none !important;
}}
header[data-testid="stHeader"] button:hover,
[data-testid="stToolbar"] button:hover,
[data-testid="stHeaderActionElements"] button:hover {{
    background-color: rgba(255, 255, 255, 0.08) !important;
    color: {PALETTE['accent_cyan']} !important;
}}
header[data-testid="stHeader"] svg,
[data-testid="stToolbar"] svg,
[data-testid="stHeaderActionElements"] svg {{
    color: #ffffff !important;
    fill: #ffffff !important;
}}

/* ---- Sidebar ---- */
section[data-testid="stSidebar"] {{
    background-color: {PALETTE['bg_panel']} !important;
    border-right: 1px solid {PALETTE['border']};
}}
section[data-testid="stSidebar"] * {{
    color: {PALETTE['text_primary']} !important;
}}

/* ---- Headings ---- */
h1, h2, h3, h4, h5, h6 {{
    color: {PALETTE['text_primary']} !important;
    font-weight: 700 !important;
    letter-spacing: -0.01em;
}}
h1 {{
    font-size: 1.8rem !important;
    border-bottom: 1px solid {PALETTE['border']};
    padding-bottom: 0.5rem;
    margin-bottom: 1rem !important;
}}

/* ---- Numbers / monospace ---- */
.mono, code, .stCode {{
    font-family: 'JetBrains Mono', 'Consolas', monospace !important;
}}

/* ---- Metric cards ---- */
[data-testid="stMetric"] {{
    background-color: {PALETTE['bg_panel']};
    border: 1px solid {PALETTE['border']};
    border-radius: 6px;
    padding: 0.75rem 1rem;
    text-align: center;
}}
[data-testid="stMetricValue"] {{
    color: {PALETTE['text_primary']} !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-weight: 700 !important;
    font-size: 1.4rem !important;
    text-align: center !important;
    justify-content: center !important;
    width: 100% !important;
}}
[data-testid="stMetricLabel"] {{
    color: {PALETTE['text_muted']} !important;
    text-transform: uppercase;
    font-size: 0.7rem !important;
    letter-spacing: 0.05em;
    text-align: center;
    justify-content: center;
    width: 100%;
}}
[data-testid="stMetricDelta"] {{
    font-family: 'JetBrains Mono', monospace !important;
    text-align: center;
    justify-content: center;
    width: 100%;
}}
/* Inner spans/divs inside the metric value also centered */
[data-testid="stMetricValue"] > div,
[data-testid="stMetricValue"] span {{
    text-align: center !important;
    width: 100% !important;
    display: block;
}}

/* ---- Buttons ----------------------------------------------------------
   Streamlit changes button DOM between versions. We use broad selectors
   covering kind="primary", data-testid="baseButton-primary",
   data-testid="stBaseButton-primary", AND popover triggers, so primary
   buttons stay consistent (cyan bg, bold black text) regardless of which
   Streamlit version is running.
   ----------------------------------------------------------------------- */

/* Default (secondary) buttons */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {{
    background-color: {PALETTE['bg_panel_2']};
    color: {PALETTE['text_primary']};
    /* Soft cyan-tinted border + glow so inactive mode buttons (and any other
       secondary button) stay clearly visible against the dark backdrop, while
       still being obviously secondary compared to a solid-cyan primary. */
    border: 1.5px solid rgba(0, 212, 255, 0.45);
    box-shadow: 0 0 0 1px rgba(0, 212, 255, 0.08);
    border-radius: 4px;
    font-weight: 600;
    transition: all 0.15s ease;
}}
.stButton > button:hover,
.stDownloadButton > button:hover,
.stFormSubmitButton > button:hover {{
    background-color: {PALETTE['accent_cyan']};
    color: #000000 !important;
    border-color: {PALETTE['accent_cyan']};
}}
.stButton > button:hover *,
.stDownloadButton > button:hover *,
.stFormSubmitButton > button:hover * {{
    color: #000000 !important;
}}

/* Primary buttons — every known DOM variant + popover triggers.
   Makes them all the same cyan with bold black text. */
button[kind="primary"],
button[data-testid="baseButton-primary"],
button[data-testid="stBaseButton-primary"],
.stButton > button[kind="primary"],
.stButton > button[data-testid="baseButton-primary"],
.stButton > button[data-testid="stBaseButton-primary"],
.stDownloadButton > button[kind="primary"],
.stDownloadButton > button[data-testid="baseButton-primary"],
.stDownloadButton > button[data-testid="stBaseButton-primary"],
.stFormSubmitButton > button[kind="primary"],
.stFormSubmitButton > button[data-testid="stBaseButton-primary"] {{
    background-color: {PALETTE['accent_cyan']} !important;
    color: #000000 !important;
    border: 1px solid {PALETTE['accent_cyan']} !important;
    border-radius: 4px !important;
    font-weight: 400 !important;
    box-shadow: none !important;
}}
button[kind="primary"]:hover,
button[data-testid="baseButton-primary"]:hover,
button[data-testid="stBaseButton-primary"]:hover,
.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover,
.stDownloadButton > button[kind="primary"]:hover,
.stDownloadButton > button[data-testid="stBaseButton-primary"]:hover {{
    background-color: {PALETTE['accent_cyan']} !important;
    color: #000000 !important;
    border-color: #ffffff !important;
    filter: brightness(1.08);
}}
/* Inner label spans inherit the bold black too */
button[kind="primary"] *,
button[data-testid="baseButton-primary"] *,
button[data-testid="stBaseButton-primary"] *,
.stButton > button[kind="primary"] *,
.stDownloadButton > button[kind="primary"] *,
.stFormSubmitButton > button[kind="primary"] * {{
    color: #000000 !important;
    font-weight: 400 !important;
    fill: #000000 !important;
}}

/* Popover triggers — same blue + bold black treatment so they look like
   "info buttons" and match the rest of the action buttons. */
[data-testid="stPopover"] button,
[data-testid="stPopoverButton"],
button[data-testid="stPopoverButton"] {{
    background-color: {PALETTE['accent_cyan']} !important;
    color: #000000 !important;
    border: 1px solid {PALETTE['accent_cyan']} !important;
    font-weight: 400 !important;
    border-radius: 4px !important;
}}
[data-testid="stPopover"] button *,
[data-testid="stPopoverButton"] *,
button[data-testid="stPopoverButton"] * {{
    color: #000000 !important;
    font-weight: 400 !important;
    fill: #000000 !important;
}}
[data-testid="stPopover"] button:hover {{
    filter: brightness(1.08);
}}

/* ---- Inputs ---- */
.stSelectbox > div, .stNumberInput > div, .stTextInput > div, .stSlider {{
    background-color: {PALETTE['bg_panel']} !important;
}}
.stSelectbox label,
.stNumberInput label,
.stTextInput label,
.stSlider label,
.stCheckbox label,
.stRadio label,
.stMultiSelect label,
.stToggle label,
[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] p,
[data-testid="stCheckbox"] label,
[data-testid="stCheckbox"] label p,
[data-testid="stRadio"] label,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] [role="radiogroup"] label,
[data-testid="stRadio"] [role="radiogroup"] label p {{
    color: {PALETTE['text_primary']} !important;
    font-size: 0.85rem !important;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    opacity: 1 !important;
}}

/* Markdown text inside checkbox/radio labels (Streamlit wraps in <p>) */
.stCheckbox label p,
.stRadio label p,
.stMultiSelect label p {{
    color: {PALETTE['text_primary']} !important;
    font-weight: 600 !important;
    opacity: 1 !important;
}}

/* Force black text inside text inputs (Streamlit's default light-on-light
   was making typed text invisible on the white input background). */
.stTextInput input,
[data-testid="stTextInput"] input,
.stNumberInput input,
[data-testid="stNumberInput"] input {{
    color: #000000 !important;
    background-color: #ffffff !important;
    font-weight: 600;
}}
.stTextInput input::placeholder,
[data-testid="stTextInput"] input::placeholder,
.stNumberInput input::placeholder {{
    color: #6b7280 !important;
    font-weight: 400;
}}

/* Hide Streamlit's "Press Enter to submit form" / "Press Enter to apply" overlay
   that floats inside text/number inputs. User finds it visually noisy. */
[data-testid="InputInstructions"],
[data-testid="stTextInputInstructions"],
[data-testid="stNumberInputInstructions"],
.stTextInput div[data-baseweb="input"] + div,
.stNumberInput div[data-baseweb="input"] + div {{
    display: none !important;
    visibility: hidden !important;
}}

/* ---- Dataframe ---- */
[data-testid="stDataFrame"] {{
    background-color: {PALETTE['bg_panel']};
    border: 1px solid {PALETTE['border']};
    border-radius: 6px;
}}

/* ---- Custom signal banner ---- */
.signal-banner {{
    padding: 1rem 1.5rem;
    border-radius: 8px;
    margin-bottom: 1rem;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    border: 1px solid;
}}
.signal-banner-green {{
    background: linear-gradient(90deg, rgba(38,217,127,0.12), rgba(38,217,127,0.04));
    border-color: {PALETTE['signal_green']};
    color: {PALETTE['signal_green']};
}}
.signal-banner-yellow {{
    background: linear-gradient(90deg, rgba(255,181,71,0.12), rgba(255,181,71,0.04));
    border-color: {PALETTE['signal_yellow']};
    color: {PALETTE['signal_yellow']};
}}
.signal-banner-red {{
    background: linear-gradient(90deg, rgba(255,77,109,0.16), rgba(255,77,109,0.04));
    border-color: {PALETTE['signal_red']};
    color: {PALETTE['signal_red']};
}}

/* ---- Custom panel ---- */
.panel {{
    background-color: {PALETTE['bg_panel']};
    border: 1px solid {PALETTE['border']};
    border-radius: 6px;
    padding: 1rem 1.25rem;
    margin-bottom: 1rem;
}}
.panel-title {{
    color: {PALETTE['text_muted']};
    text-transform: uppercase;
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    font-weight: 600;
    margin-bottom: 0.5rem;
}}

/* ---- Pill / tag ---- */
.pill {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    font-weight: 600;
    margin-right: 4px;
}}
.pill-green {{ background-color: rgba(38,217,127,0.18); color: {PALETTE['up_green']}; border: 1px solid {PALETTE['up_green']}; }}
.pill-red   {{ background-color: rgba(255,77,109,0.18); color: {PALETTE['down_red']}; border: 1px solid {PALETTE['down_red']}; }}
.pill-cyan  {{ background-color: rgba(0,212,255,0.16); color: {PALETTE['accent_cyan']}; border: 1px solid {PALETTE['accent_cyan']}; }}
.pill-gray  {{ background-color: {PALETTE['bg_panel_2']}; color: {PALETTE['text_muted']}; border: 1px solid {PALETTE['border']}; }}

/* ---- Tabs ---- */
.stTabs [data-baseweb="tab-list"] {{
    background-color: {PALETTE['bg_main']};
    border-bottom: 1px solid {PALETTE['border']};
    gap: 0;
}}
.stTabs [data-baseweb="tab"] {{
    background-color: transparent;
    color: {PALETTE['text_muted']};
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.8rem;
    padding: 0.5rem 1rem;
}}
.stTabs [aria-selected="true"] {{
    color: {PALETTE['accent_cyan']} !important;
    border-bottom: 2px solid {PALETTE['accent_cyan']};
}}

/* ---- Expander ---- */
.streamlit-expanderHeader {{
    background-color: {PALETTE['bg_panel']} !important;
    color: {PALETTE['text_primary']} !important;
    border: 1px solid {PALETTE['border']};
    border-radius: 4px;
}}

/* ---- Tooltip "?" icons (info circles next to widget labels) -----------
   Previous attempt to redesign these as black circles with a white "i"
   inside accidentally squished every button that had help text, because
   [data-testid="stTooltipIcon"] sits on more elements than just the icon
   in this Streamlit version. Reverting to a safe SVG-color-only approach:
   we only restyle the SVG glyph itself, never the container. Icon stays
   as the default question mark but cyan-tinted and slightly enlarged.
   ---------------------------------------------------------------------- */
[data-testid="stTooltipIcon"] svg,
[data-testid="stTooltipHoverTarget"] svg,
.stTooltipIcon svg,
[data-baseweb="tooltip"] svg {{
    color: {PALETTE['accent_cyan']} !important;
    fill: {PALETTE['accent_cyan']} !important;
    stroke: {PALETTE['accent_cyan']} !important;
    opacity: 1 !important;
    width: 18px !important;
    height: 18px !important;
}}

/* ---- Sidebar collapse / chevron icons (white instead of dark) ---- */
[data-testid="stSidebarCollapsedControl"] svg,
[data-testid="stSidebarCollapseButton"] svg,
[data-testid="stSidebarHeader"] svg,
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="collapsedControl"] svg,
[data-testid="baseButton-headerNoPadding"] svg,
button[kind="header"] svg,
button[data-testid="baseButton-header"] svg {{
    color: {PALETTE['text_primary']} !important;
    fill: {PALETTE['text_primary']} !important;
    stroke: {PALETTE['text_primary']} !important;
}}
[data-testid="stSidebarCollapsedControl"]:hover svg,
[data-testid="stSidebarCollapseButton"]:hover svg {{
    color: {PALETTE['accent_cyan']} !important;
    fill: {PALETTE['accent_cyan']} !important;
}}

/* ---- Scrollbars ---- */
::-webkit-scrollbar {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track {{ background: {PALETTE['bg_main']}; }}
::-webkit-scrollbar-thumb {{ background: {PALETTE['border']}; border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: {PALETTE['text_muted']}; }}

/* ---- Progress bar ---- */
.stProgress > div > div > div > div {{ background-color: {PALETTE['accent_cyan']}; }}

/* ---- Dividers ---- */
hr {{ border-color: {PALETTE['border']} !important; margin: 1.5rem 0 !important; }}
</style>
"""


def apply_theme():
    """Inject the dark Bloomberg theme. Call once per page."""
    st.markdown(_CSS, unsafe_allow_html=True)


def signal_banner(signal: str, subtitle: str = "") -> str:
    """Build HTML for a color-coded market signal banner."""
    if signal.startswith("GREEN"):
        cls = "signal-banner-green"
        icon = "▲"
    elif signal.startswith("YELLOW"):
        cls = "signal-banner-yellow"
        icon = "◆"
    elif signal.startswith("RED"):
        cls = "signal-banner-red"
        icon = "▼"
    else:
        cls = "signal-banner-yellow"
        icon = "•"
    sub_html = f"<div style='font-size:0.85rem;font-weight:400;margin-top:0.3rem;opacity:0.85'>{subtitle}</div>" if subtitle else ""
    return f"""
    <div class="signal-banner {cls}">
      <div style="font-size:1.05rem;letter-spacing:0.05em;">
        {icon} &nbsp; MARKET SIGNAL: {signal}
      </div>
      {sub_html}
    </div>
    """


def panel(title: str, body_html: str) -> str:
    """Build HTML for a titled dark panel. Body should already be HTML."""
    return (
        f"<div class='panel'>"
        f"<div class='panel-title'>{title}</div>"
        f"{body_html}"
        f"</div>"
    )


def pill(text: str, color: str = "gray") -> str:
    """Build HTML for a colored pill / tag.

    color: 'green', 'red', 'cyan', 'gray', 'yellow' (yellow maps to gray)
    """
    color_key = color if color in ("green", "red", "cyan", "gray") else "gray"
    return f"<span class='pill pill-{color_key}'>{text}</span>"
