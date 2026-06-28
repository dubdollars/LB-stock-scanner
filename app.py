"""
Legacy Brands Money Making Machine — main Streamlit app.

Run: streamlit run app.py
Or:  double-click run.bat (Windows)
"""

from __future__ import annotations

import hmac
import io
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Make sure local packages import correctly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scanner import ScanSettings, run_scan, save_scan, load_latest_scan, list_scans
from scanner.social_scan import (
    SocialScanSettings, run_social_scan, load_latest_social_snapshot,
)
from ui.style import apply_theme, signal_banner, panel, pill, PALETTE
from ui.deepdive_view import render_deep_dive
from ui.excel_export import format_dataframe_to_excel
from ui.quality_view import render_quality_page, render_quality_sidebar
from ui.top_stocks_view import render_top_stocks_page, render_top_stocks_sidebar
from ui.journal_view import render_journal_page, render_journal_sidebar
from ui.mean_reversion_view import render_mean_reversion_page, render_mean_reversion_sidebar


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Legacy Brands Money Making Machine",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_theme()


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------
# Reads the password from st.secrets["app_password"], which is set via:
#   - local:  .streamlit/secrets.toml   (gitignored)
#   - cloud:  Streamlit Cloud's secrets UI
# If no password is configured at all, the gate is bypassed (so first-time
# local runs work without setup).

def _check_password() -> bool:
    """Render the login screen until the correct password is entered."""
    try:
        expected = st.secrets.get("app_password", "")
    except Exception:
        expected = ""

    # No password configured -> open access (useful for first-time local runs)
    if not expected:
        return True

    if st.session_state.get("password_correct"):
        return True

    def _verify():
        entered = st.session_state.get("_password_input", "")
        if hmac.compare_digest(str(entered), str(expected)):
            st.session_state.password_correct = True
            st.session_state.pop("_password_input", None)
        else:
            st.session_state.password_correct = False

    # Login screen
    st.markdown(
        f"<div style='text-align:center;margin-top:6rem;'>"
        f"<div style='font-family:JetBrains Mono;font-size:3rem;font-weight:700;color:{PALETTE['accent_cyan']}'>◆</div>"
        f"<h1 style='border:none;margin-top:0.5rem'>Legacy Brands Money Making Machine</h1>"
        f"<div style='color:{PALETTE['text_muted']};font-size:0.9rem;margin-bottom:2rem'>"
        f"Password required to access this dashboard."
        f"</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.text_input(
            "Password",
            type="password",
            key="_password_input",
            on_change=_verify,
            label_visibility="collapsed",
            placeholder="Enter password…",
        )
        if st.session_state.get("password_correct") is False:
            st.error("Incorrect password. Try again.")
    return False


if not _check_password():
    st.stop()


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

def _init_state():
    # Top-level MODE: 'longs' or 'shorts'. Determines which sidebar + which
    # default view is shown. Two big buttons at the top of every page switch
    # between them.
    # Default mode + view = ⭐ TOP STOCKS (the new home page).
    # Top Stocks is the intersection of the Long + Quality scans — only the
    # very best stocks show up here. This is what the user sees when the
    # app opens.
    if "mode" not in st.session_state:
        st.session_state.mode = "top_stocks"
    if "view" not in st.session_state:
        st.session_state.view = "top_stocks"
    if "deep_dive_ticker" not in st.session_state:
        st.session_state.deep_dive_ticker = None
    if "settings" not in st.session_state:
        st.session_state.settings = ScanSettings()
    if "latest_scan" not in st.session_state:
        st.session_state.latest_scan = load_latest_scan()
    if "research_ticker" not in st.session_state:
        st.session_state.research_ticker = None
    if "rotation_selected_industry" not in st.session_state:
        st.session_state.rotation_selected_industry = None
    if "social_settings" not in st.session_state:
        st.session_state.social_settings = SocialScanSettings()
    if "latest_social_scan" not in st.session_state:
        st.session_state.latest_social_scan = load_latest_social_snapshot()


def _render_mode_switcher():
    """Mode buttons at the top of every page.
    The currently-active mode renders as a solid cyan PRIMARY button; the
    others render as dark SECONDARY buttons. The contrast makes the active
    mode obvious at a glance. A small "VIEWING: …" caption underneath also
    spells it out in writing for absolute clarity.
    """
    current = st.session_state.get("mode", "top_stocks")

    def _btn_type(mode_name: str) -> str:
        return "primary" if current == mode_name else "secondary"

    # Display order: TOP STOCKS · MOMENTUM · QUALITY · MEAN REVERSION · SOCIAL · ROTATION · JOURNAL · RESEARCH.
    # MEAN REVERSION sits next to QUALITY because it consumes the Quality scan
    # (its universe = Quality candidates with Composite ≥ 70).
    # TOP STOCKS is leftmost AND the default landing page (intersection of momentum + quality).
    # JOURNAL is the trade-tracking workspace (used after entries).
    # RESEARCH is the catch-all deep-dive — rightmost.
    # EARLY mode was removed 2026-06-11 (was not producing reliable signals).
    # SHORTS was removed 2026-06-06.
    # Internal mode key for the social mode stays "twitter" so existing
    # session state / snapshots keep working — only the LABEL is "SOCIAL".
    # Equal column widths for every mode button so the chips look uniform.
    # MEAN REVERSION's two-word label wraps to a second line at this width;
    # that's better than making one button noticeably wider than the rest.
    (col_top, col_long, col_quality, col_mr, col_social, col_rotation,
     col_journal, col_research, col_spacer) = st.columns(
        [1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.0]
    )
    with col_top:
        if st.button("⭐ TOP STOCKS", type=_btn_type("top_stocks"),
                     use_container_width=True, key="mode_btn_top_stocks"):
            st.session_state.mode = "top_stocks"
            st.session_state.view = "top_stocks"
            st.session_state.research_ticker = None
            st.rerun()
    with col_long:
        if st.button("📈 MOMENTUM", type=_btn_type("longs"),
                     use_container_width=True, key="mode_btn_longs"):
            st.session_state.mode = "longs"
            st.session_state.view = "dashboard"
            st.session_state.research_ticker = None
            st.rerun()
    with col_quality:
        if st.button("💎 QUALITY", type=_btn_type("quality"),
                     use_container_width=True, key="mode_btn_quality"):
            st.session_state.mode = "quality"
            st.session_state.view = "quality"
            st.session_state.research_ticker = None
            st.rerun()
    with col_mr:
        # Label kept short ("MEAN REV") so the button stays on one line and
        # matches the height of its neighbors. Full "Mean Reversion" name still
        # appears in the page header, sidebar, and TOC dialog.
        if st.button("🔁 MEAN REV", type=_btn_type("mean_reversion"),
                     use_container_width=True, key="mode_btn_mean_reversion"):
            st.session_state.mode = "mean_reversion"
            st.session_state.view = "mean_reversion"
            st.session_state.research_ticker = None
            st.rerun()
    with col_social:
        if st.button("🐦 SOCIAL", type=_btn_type("twitter"),
                     use_container_width=True, key="mode_btn_twitter"):
            st.session_state.mode = "twitter"
            st.session_state.view = "twitter"
            st.session_state.research_ticker = None
            st.rerun()
    with col_rotation:
        if st.button("🔄 ROTATION", type=_btn_type("rotation"),
                     use_container_width=True, key="mode_btn_rotation"):
            st.session_state.mode = "rotation"
            st.session_state.view = "rotation"
            st.session_state.research_ticker = None
            st.session_state.rotation_selected_industry = None
            st.rerun()
    with col_journal:
        if st.button("📓 JOURNAL", type=_btn_type("journal"),
                     use_container_width=True, key="mode_btn_journal"):
            st.session_state.mode = "journal"
            st.session_state.view = "journal"
            st.session_state.research_ticker = None
            st.rerun()
    with col_research:
        if st.button("🔎 RESEARCH", type=_btn_type("research"),
                     use_container_width=True, key="mode_btn_research"):
            st.session_state.mode = "research"
            st.session_state.view = "research"
            st.session_state.research_ticker = None
            st.rerun()

    # Belt-and-braces: small text caption so the active mode is also in writing
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    mode_pretty = {
        "top_stocks": "TOP STOCKS",
        "journal": "JOURNAL",
        "longs": "MOMENTUM",
        "research": "RESEARCH", "twitter": "SOCIAL",
        "rotation": "ROTATION", "quality": "QUALITY",
        "mean_reversion": "MEAN REVERSION",
    }.get(current, current.upper())
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
        f"letter-spacing:0.1em;margin-top:0.25rem;margin-bottom:0.5rem'>"
        f"VIEWING: <b style='color:{cyan}'>{mode_pretty}</b></div>",
        unsafe_allow_html=True,
    )
    st.markdown("<hr style='margin-top:0.25rem;margin-bottom:0.75rem'>", unsafe_allow_html=True)


_init_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_utc_iso(iso_str: str):
    """Parse a stored UTC ISO timestamp (with or without trailing 'Z') into a
    tz-aware datetime. Returns None on parse failure."""
    from datetime import timezone as _tz
    if not iso_str:
        return None
    try:
        s = iso_str
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # If the parsed time has no tz info, assume it was UTC (legacy timestamps)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt
    except Exception:
        return None


def _format_timestamp(iso_str: str) -> str:
    """Render a UTC ISO timestamp in the USER\'S LOCAL TIMEZONE.

    Uses datetime.astimezone() with no argument, which converts to the
    machine\'s configured local timezone. On the user\'s Windows machine
    this is EST/EDT automatically (Windows handles DST). The displayed
    label uses the local timezone abbreviation (e.g. EST, EDT)."""
    dt_utc = _parse_utc_iso(iso_str)
    if dt_utc is None:
        return "never"
    try:
        dt_local = dt_utc.astimezone()  # convert to system local time
        tz_label = dt_local.tzname() or ""  # e.g. "EST" / "Eastern Standard Time"
        # Windows tznames are sometimes verbose ("Eastern Standard Time" vs "EST").
        # Compress multi-word names to their initials so the label stays short.
        if " " in tz_label:
            tz_label = "".join(w[0] for w in tz_label.split() if w)
        return dt_local.strftime("%Y-%m-%d %H:%M ") + tz_label
    except Exception:
        return iso_str


def _scan_age_string(iso_str: str) -> str:
    """Age of a scan, using tz-aware UTC comparison so DST changes don\'t lie."""
    from datetime import datetime as _dt, timezone as _tz
    dt = _parse_utc_iso(iso_str)
    if dt is None:
        return ""
    try:
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
# Keep Windows awake during long scans
# ---------------------------------------------------------------------------
# When the laptop sleeps mid-scan, Python pauses and yfinance loses its
# network connections — the scan effectively stops. SetThreadExecutionState
# tells Windows "don't sleep while I'm working." We call _keep_awake() at the
# start of a scan and _allow_sleep() in the finally block to restore normal
# power management. No-op on non-Windows platforms.

def _keep_awake():
    """Tell the OS not to sleep while a scan is running."""
    if sys.platform == "win32":
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            # System stays awake; display CAN turn off (the scan doesn't
            # need the display, and keeping the display on for 25 minutes
            # would waste a lot of battery).
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
        except Exception:
            pass  # best-effort — fall back to OS defaults


def _allow_sleep():
    """Restore normal power management after the scan finishes."""
    if sys.platform == "win32":
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass


def _trigger_rescan(settings: ScanSettings, demo: bool = False):
    """Run a scan with a live progress UI. Stores result in session_state and saves to cache."""
    progress_container = st.empty()
    bar = st.progress(0.0, text="Starting…")
    status = st.empty()
    _keep_awake()

    if demo:
        settings.ticker_subset = [
            "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
            "AMD", "NFLX", "ORCL", "CRM", "ADBE", "PLTR", "SNOW", "CRWD",
            "PANW", "DDOG", "MDB", "NET",
        ]

    def progress_cb(phase: str, current: int, total: int, message: str):
        pct = (current / total) if total else 0.0
        pct = max(0.0, min(1.0, pct))
        # Map sub-phases into a global 0-1 bar (rough weighting)
        weights = {"market": 0.05, "tickers": 0.05, "spy": 0.05,
                   "history": 0.40, "breadth": 0.05,
                   "fundamentals": 0.30, "filter": 0.10, "done": 0.0}
        order = ["market", "tickers", "spy", "history", "breadth", "fundamentals", "filter", "done"]
        phase_idx = order.index(phase) if phase in order else 0
        completed_weight = sum(weights[order[i]] for i in range(phase_idx))
        global_pct = completed_weight + weights.get(phase, 0) * pct
        bar.progress(min(1.0, global_pct), text=f"[{phase.upper()}] {message}")

    try:
        result = run_scan(settings, progress=progress_cb)
        save_scan(result)
        st.session_state.latest_scan = result.to_dict()
        bar.progress(1.0, text=f"✓ Scan complete — {len(result.candidates)} candidates")
        status.success(f"Found {len(result.candidates)} candidates from {result.universe_size} stocks scanned.")
    except Exception as e:
        status.error(f"Scan failed: {e}")
        raise
    finally:
        progress_container.empty()
        _allow_sleep()


def _trigger_social_rescan(settings: SocialScanSettings):
    """Run a fresh social scan and update session state. UI-thin wrapper that
    surfaces progress + errors via the same Streamlit primitives used by the
    long-side rescan helper. Sleep-prevention added 2026-06-15 for parity
    with all other scan runners — social is usually ~30-60 sec but the
    override is cheap insurance if Apewisdom or yfinance hangs."""
    progress_container = st.empty()
    bar = progress_container.progress(0.0, text="Starting social scan...")
    status = st.empty()
    _keep_awake()

    PHASES = ["apewisdom", "enrich", "spike", "done"]

    def progress_cb(phase: str, idx: int, total: int, message: str):
        try:
            base = PHASES.index(phase) / max(1, len(PHASES))
        except ValueError:
            base = 0
        slot = 1.0 / len(PHASES)
        within = (idx / total) if total else 0.0
        global_pct = base + slot * within
        bar.progress(min(1.0, global_pct), text=f"[SOCIAL · {phase.upper()}] {message}")

    try:
        result = run_social_scan(settings, progress=progress_cb)
        st.session_state.latest_social_scan = result.to_dict()
        bar.progress(1.0, text=f"✓ Social scan complete — {len(result.candidates)} tickers")
        status.success(
            f"Found {len(result.candidates)} trending tickers (universe: {result.universe_size})."
        )
    except Exception as e:
        status.error(f"Social scan failed: {e}")
        raise
    finally:
        progress_container.empty()
        _allow_sleep()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_long_sidebar():
    """Long-scan sidebar — title, Elite mode, action buttons (Start Scan +
    downloads + Top Stocks + TOC), Check Ticker, then LAST SCAN at the bottom.
    Last Scan is moved to the bottom per user request — primary actions sit
    near the top of the rail where they\'re easiest to reach.
    """
    cyan = PALETTE['accent_cyan']
    muted = PALETTE['text_muted']
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{cyan};margin-bottom:0.5rem;line-height:1.2'>💰 LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;color:{cyan};margin-bottom:0.5rem'>MONEY MAKING MACHINE</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.25rem'>SEPA momentum scanner</div>",
        unsafe_allow_html=True,
    )

    # ---- Elite-mode toggle (moved to top) ----
    settings: ScanSettings = st.session_state.settings
    settings.elite_mode = st.toggle(
        "Elite mode (RS≥85, EPS required)",
        value=settings.elite_mode,
        help="OFF = Grand Master (RS≥70, broader list including stocks without strong fundamentals)",
    )
    st.session_state.settings = settings

    # ---- Start Scan button ----
    st.markdown("<div style='margin-top:0.5rem'></div>", unsafe_allow_html=True)
    if st.button("▶ Run Scan", type="primary", use_container_width=True,
                 help="Run a full ~25-minute MOMENTUM scan against the S&P 500 + Nasdaq 100 + Russell 2000 + Russell 3000 universe"):
        _trigger_rescan(settings, demo=False)
        st.rerun()

    # ---- Stacked action buttons (full-width, primary blue) ----
    filtered_df = st.session_state.get("filtered_candidates")
    has_results = filtered_df is not None and len(filtered_df) > 0

    if has_results:
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                filtered_df,
                sheet_name="Candidates",
                title=f"Legacy Brands - Momentum Candidates {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"legacy_brands_scan_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")

        tickers_text = "\n".join(filtered_df["Ticker"].astype(str).tolist())
        stamp = datetime.utcnow().strftime("%d%b%Y")
        st.download_button(
            label="📥 Download Tickers (.txt)",
            data=tickers_text,
            file_name=f"legacy_brands_tickers_{stamp}.txt",
            mime="text/plain",
            use_container_width=True,
            type="primary",
        )

        # Note: the old "⭐ Top Stocks" button used to live here. It now
        # lives as the leftmost top-level mode button and is the app's
        # default home page — see ui/top_stocks_view.py.

        if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                     help="Definitions for every momentum-scan column and signal/warning tag"):
            _show_toc_dialog()

        st.caption(f"Exports include the {len(filtered_df)} rows currently visible (after filters).")
    else:
        st.caption("Run a scan to enable exports.")

    st.markdown("---")

    # ---- Check Ticker diagnostic ----
    st.markdown(f"<div class='panel-title'>CHECK TICKER</div>", unsafe_allow_html=True)
    with st.form("diagnose_form", clear_on_submit=False):
        check_ticker = st.text_input(
            "Symbol to diagnose",
            placeholder="e.g. NVDA",
            label_visibility="collapsed",
            help="Type any ticker and hit Enter (or click Diagnose) to see which SEPA tests it passes/fails.",
        )
        submitted = st.form_submit_button("🔍 Diagnose", type="primary", use_container_width=True)
        if submitted and check_ticker:
            st.session_state.diagnose_ticker = check_ticker.upper().strip()
            st.session_state.view = "diagnose"
            st.rerun()

    st.markdown("---")

    # ---- Last Scan box (moved to the bottom) ----
    latest = st.session_state.latest_scan
    if latest:
        age = _scan_age_string(latest.get("timestamp", ""))
        stamp = _format_timestamp(latest.get("timestamp", ""))
        st.markdown(
            panel("LAST SCAN",
                  f"<div style='font-family:JetBrains Mono;font-size:0.85rem'>{stamp}<br>"
                  f"<span style='color:{cyan}'>{age}</span> &middot; "
                  f"{len(latest.get('candidates', []))} candidates</div>"),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            panel("LAST SCAN", f"<div style='color:{muted}'>No scan yet — use Start Scan above.</div>"),
            unsafe_allow_html=True,
        )


def _render_twitter_sidebar():
    """Twitter-mode sidebar — Refresh, Download Excel + Tickers, Table of Contents,
    LAST SOCIAL SCAN box at the bottom (mirrors the long sidebar layout)."""
    cyan = PALETTE['accent_cyan']
    muted = PALETTE['text_muted']
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{cyan};margin-bottom:0.5rem;line-height:1.2'>🐦 LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;color:{cyan};margin-bottom:0.5rem'>SOCIAL MOMENTUM</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.25rem'>Twitter / Reddit / StockTwits trending tickers</div>",
        unsafe_allow_html=True,
    )

    # ---- Refresh button (primary action) ----
    social_settings: SocialScanSettings = st.session_state.social_settings
    if st.button("▶ Run Scan", type="primary", use_container_width=True,
                 help="Fetch the current top-100 trending tickers from Apewisdom, "
                      "apply price/volume guardrails, enrich with technicals + fundamentals, "
                      "save a snapshot. Takes ~30-60 seconds with warm caches."):
        _trigger_social_rescan(social_settings)
        st.rerun()

    # ---- Stacked action buttons (visible once we have results) ----
    filtered_df = st.session_state.get("filtered_social_candidates")
    has_results = filtered_df is not None and len(filtered_df) > 0

    if has_results:
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                filtered_df,
                sheet_name="Social",
                title=f"Legacy Brands - Social Momentum {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"legacy_brands_social_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")

        tickers_text = "\n".join(filtered_df["Ticker"].astype(str).tolist())
        stamp = datetime.utcnow().strftime("%d%b%Y")
        st.download_button(
            label="📥 Download Tickers (.txt)",
            data=tickers_text,
            file_name=f"legacy_brands_social_tickers_{stamp}.txt",
            mime="text/plain",
            use_container_width=True,
            type="primary",
        )

        if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                     help="Definitions for every social-scan column and signal"):
            _show_social_toc_dialog()

        st.caption(f"Exports include the {len(filtered_df)} rows currently visible (after filters).")
    else:
        st.caption("Click Refresh to pull the current trending tickers.")

    st.markdown("---")

    # ---- Last Social Scan box (bottom — same pattern as long sidebar) ----
    latest = st.session_state.latest_social_scan
    if latest:
        age = _scan_age_string(latest.get("timestamp", ""))
        stamp_pretty = _format_timestamp(latest.get("timestamp", ""))
        st.markdown(
            panel("LAST SOCIAL SCAN",
                  f"<div style='font-family:JetBrains Mono;font-size:0.85rem'>{stamp_pretty}<br>"
                  f"<span style='color:{cyan}'>{age}</span> &middot; "
                  f"{len(latest.get('candidates', []))} tickers</div>"),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            panel("LAST SOCIAL SCAN", f"<div style='color:{muted}'>No social scan yet — use Refresh above.</div>"),
            unsafe_allow_html=True,
        )


def _render_sidebar():
    """Dispatch to the correct mode-specific sidebar."""
    mode = st.session_state.get("mode", "longs")
    view = st.session_state.get("view", "dashboard")
    with st.sidebar:
        # Universal "Return to Home" — shown everywhere except the TOP STOCKS
        # page (which IS home). Routes back to the Top Stocks intersection
        # page (the app's new default landing page).
        is_home = (mode == "top_stocks" and view == "top_stocks")
        if not is_home:
            if st.button("← Return to Home",
                         key="universal_home_btn",
                         type="primary",
                         use_container_width=True):
                st.session_state.mode = "top_stocks"
                st.session_state.view = "top_stocks"
                st.session_state.deep_dive_ticker = None
                st.session_state.deep_dive_origin = None
                st.rerun()
            st.markdown("---")

        if mode == "top_stocks":
            render_top_stocks_sidebar()
        elif mode == "journal":
            render_journal_sidebar()
        elif mode == "research":
            _render_research_sidebar()
        elif mode == "twitter":
            _render_twitter_sidebar()
        elif mode == "rotation":
            _render_rotation_sidebar()
        elif mode == "quality":
            render_quality_sidebar()
        elif mode == "mean_reversion":
            render_mean_reversion_sidebar()
        else:
            _render_long_sidebar()

# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------

def _signal_to_class(signal: str) -> str:
    if signal.startswith("GREEN"):
        return "green"
    if signal.startswith("YELLOW"):
        return "yellow"
    return "red"


def _render_market_strip(scan: dict):
    sig = scan.get("market_signal", "N/A")
    subtitle = (
        f"S&P 500: <b>{scan.get('sp_trend', 'N/A')}</b> · Dist days: {scan.get('sp_dist', 0)} &nbsp;|&nbsp; "
        f"Nasdaq: <b>{scan.get('nq_trend', 'N/A')}</b> · Dist days: {scan.get('nq_dist', 0)} &nbsp;|&nbsp; "
        f"Russell 2000: <b>{scan.get('rut_trend', 'N/A')}</b> · Dist days: {scan.get('rut_dist', 0)}"
    )
    st.markdown(signal_banner(sig, subtitle), unsafe_allow_html=True)


_METRIC_DOCS = {
    "UNIVERSE": (
        "**Total stocks scanned in this run.**\n\n"
        "The universe is S&P 500 + Nasdaq 100 + Russell 2000 + Russell 3000 holdings, "
        "deduplicated, after filtering by minimum price and minimum 50-day average volume. "
        "Typically ~2500-3500 names depending on liquidity.\n\n"
        "If this number is unusually low, Yahoo Finance may have rate-limited some downloads."
    ),
    "> 50-DAY MA": (
        "**% of stocks trading above their 50-day moving average.**\n\n"
        "Short-term breadth indicator — how broadly stocks are participating in the trend.\n\n"
        "**Note:** this is BREADTH (% of stocks), not individual-stock RSI. The "
        "two are different metrics that happen to share a 0-100 scale.\n\n"
        "**Interpretation:**\n"
        "- 90%+ = market-wide overbought (mean-reversion risk)\n"
        "- 70-90% = strongly bullish, market is broad\n"
        "- 50-70% = healthy uptrend\n"
        "- 30-50% = mixed / consolidation\n"
        "- Under 30% = bearish, narrow leadership"
    ),
    "> 200-DAY MA": (
        "**% of stocks trading above their 200-day moving average.**\n\n"
        "Long-term / structural bull-bear indicator. The 200-day separates "
        "secular bull markets from bear markets at the individual-stock level.\n\n"
        "**Interpretation:**\n"
        "- 80%+ = late-cycle overbought (historically near tops)\n"
        "- 60-80% = secular bull market intact\n"
        "- 40-60% = mixed regime\n"
        "- Under 40% = many stocks in bear-market territory"
    ),
    "UP/DN VOL": (
        "**Up volume divided by down volume across the universe today.**\n\n"
        "= (volume on stocks closing up) ÷ (volume on stocks closing down).\n\n"
        "**Interpretation:**\n"
        "- 1.5+ = strong accumulation (institutions buying)\n"
        "- 1.0-1.5 = healthy net buying\n"
        "- 0.7-1.0 = mild distribution\n"
        "- Under 0.7 = heavy distribution (institutions selling)"
    ),
    "NEW HIGHS": (
        "**Stocks closing at a TRUE new 52-week high vs a true new 52-week low.**\n\n"
        "Strict NYSE-style definition: today's close must exceed every close in the "
        "prior 251 trading sessions to count as a new high (vice versa for new lows). "
        "No ±1% tolerance — that loose version is sensitive to anniversary-roll-off "
        "noise from the trailing 52-week window.\n\n"
        "Healthy bull markets have new-highs >> new-lows. When new-lows start dominating, "
        "leadership is breaking down."
    ),
    "CANDIDATES": (
        "**Stocks that passed every test in this scan.**\n\n"
        "Includes: trend template (price > 50/150/200 MAs in the right order, near 52w high, "
        "200MA trending up), RS threshold (≥85 Elite / ≥70 Standard), and (in Elite mode) "
        "fundamentals (EPS+ or Sale+).\n\n"
        "VDU and VCP are informational tags, not filters. SIGNALS / WARNINGS columns "
        "characterize each candidate further."
    ),
}


def _md_to_tooltip_html(md: str) -> str:
    """Tiny markdown → HTML converter for hover tooltips.

    Handles only what _METRIC_DOCS actually uses: **bold**, blank-line
    paragraphs, and `- ` bullet lists. Avoids pulling in a markdown dep.
    """
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


def _render_breadth_metrics(scan: dict):
    """Six metric cards in a CSS grid. Hover any card to see its explanation."""
    metrics_data = [
        ("UNIVERSE", f"{scan.get('universe_size', 0):,}", None, "neutral"),
        ("> 50-DAY MA", f"{scan.get('pct_above_50', 0):.1f}%",
         "↑ Healthy" if scan.get('pct_above_50', 0) > 50 else "↓ Weak",
         "up" if scan.get('pct_above_50', 0) > 50 else "down"),
        ("> 200-DAY MA", f"{scan.get('pct_above_200', 0):.1f}%",
         "↑ Bull" if scan.get('pct_above_200', 0) > 50 else "↓ Bear",
         "up" if scan.get('pct_above_200', 0) > 50 else "down"),
        ("UP/DN VOL", f"{scan.get('up_down_ratio', 0):.2f}",
         "↑ Accumulation" if scan.get('up_down_ratio', 0) > 1 else "↑ Distribution",
         "up" if scan.get('up_down_ratio', 0) > 1 else "down"),
        ("NEW HIGHS", f"{scan.get('new_highs', 0)}",
         f"↑ vs {scan.get('new_lows', 0)} lows",
         "up" if scan.get('new_highs', 0) > scan.get('new_lows', 0) else "down"),
        ("CANDIDATES", f"{len(scan.get('candidates', []))}", None, "neutral"),
    ]

    # Inline CSS once per render — cheap & keeps the change local to this widget.
    css = """
<style>
.breadth-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 0.8rem 0;
}
.breadth-card {
    position: relative;
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: center;
    cursor: help;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.breadth-card:hover {
    border-color: #00D4FF;
    box-shadow: 0 0 0 1px rgba(0,212,255,0.25);
}
.breadth-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.breadth-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
.breadth-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    margin-top: 0.25rem;
    font-weight: 600;
}
.breadth-delta.up   { color: #00FF88; }
.breadth-delta.down { color: #FF4D6D; }
.breadth-tooltip {
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
.breadth-card:hover .breadth-tooltip {
    opacity: 1;
    visibility: visible;
}
.breadth-tooltip-title {
    font-family: 'JetBrains Mono', monospace;
    color: #00D4FF;
    font-size: 0.85rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
    letter-spacing: 0.04em;
}
/* keep tooltip on-screen for the right-most cards */
.breadth-grid .breadth-card:nth-child(5) .breadth-tooltip,
.breadth-grid .breadth-card:nth-child(6) .breadth-tooltip {
    left: auto;
    right: 0;
    transform: none;
}
.breadth-grid .breadth-card:nth-child(1) .breadth-tooltip,
.breadth-grid .breadth-card:nth-child(2) .breadth-tooltip {
    left: 0;
    transform: none;
}
</style>
"""

    cards_html = []
    for label, value, delta, delta_class in metrics_data:
        delta_html = (
            f"<div class='breadth-delta {delta_class}'>{delta}</div>"
            if delta is not None else "<div class='breadth-delta'>&nbsp;</div>"
        )
        doc_html = _md_to_tooltip_html(_METRIC_DOCS.get(label, ""))
        cards_html.append(
            f"<div class='breadth-card'>"
            f"<div class='breadth-label'>{label}</div>"
            f"<div class='breadth-value'>{value}</div>"
            f"{delta_html}"
            f"<div class='breadth-tooltip'>"
            f"<div class='breadth-tooltip-title'>{label}</div>"
            f"{doc_html}"
            f"</div>"
            f"</div>"
        )

    st.markdown(css + "<div class='breadth-grid'>" + "".join(cards_html) + "</div>",
                unsafe_allow_html=True)

    breadth = scan.get("breadth_status", "")
    color = _signal_to_class(breadth)
    explanation = _breadth_explanation(scan)
    explanation_html = (
        f"<div style='font-family:Inter;font-size:0.8rem;font-weight:400;margin-top:0.3rem;opacity:0.95'>{explanation}</div>"
        if explanation else ""
    )
    st.markdown(
        f"<div class='signal-banner signal-banner-{color}' style='padding:0.6rem 1rem;font-size:0.85rem'>"
        f"<b>INTERNAL BREADTH: {breadth}</b>"
        f"{explanation_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


def _breadth_explanation(scan: dict) -> str:
    """Return a one-line explanation of WHY the breadth got its current rating,
    citing the actual underlying metrics."""
    status = scan.get("breadth_status", "")
    pct_50 = scan.get("pct_above_50", 0) or 0
    pct_50_10d = scan.get("pct_above_50_10d_ago", 0) or 0
    udr = scan.get("up_down_ratio", 0) or 0
    nh = scan.get("new_highs", 0) or 0
    nl = scan.get("new_lows", 0) or 0
    sp_trend = scan.get("sp_trend", "N/A")
    nq_trend = scan.get("nq_trend", "N/A")
    rut_trend = scan.get("rut_trend", "N/A")

    if "New Lows" in status:
        return (
            f"{nl} stocks at new 52-week lows vs only {nh} at new highs — "
            "leadership is breaking down. Defensive posture warranted."
        )
    if "Broad Weakness" in status:
        return (
            f"Up/Down volume {udr:.2f} (selling > buying) AND only {pct_50:.0f}% "
            "of stocks above their 50-day MA — institutional distribution across "
            "the market."
        )
    if "Small Caps Lagging" in status:
        return (
            f"S&P and Nasdaq are UP but Russell 2000 is {rut_trend} — large caps "
            "are masking weakness underneath. Classic late-cycle narrowing. "
            "Be selective; favour the strongest leaders only."
        )
    if "Participation Falling" in status:
        delta = pct_50_10d - pct_50
        return (
            f"% of stocks above their 50-day MA has dropped from "
            f"{pct_50_10d:.0f}% to {pct_50:.0f}% over the last 10 sessions "
            f"({delta:.0f}pp decline). Fewer names are participating even if "
            "the headline indices look flat. Tighten stops, raise the bar on new entries."
        )
    if "Speculative Bounce" in status:
        return (
            f"Breadth looks healthy ({pct_50:.0f}% above 50MA, U/D ratio {udr:.2f}), "
            f"but primary index trend has broken (S&P: {sp_trend}, Nasdaq: {nq_trend}). "
            "Bounces in confirmed downtrends rarely persist — don't get aggressive."
        )
    if "Trend Broken" in status:
        return (
            f"Major index has rolled over (S&P: {sp_trend}, Nasdaq: {nq_trend}). "
            "Even if individual breadth still looks decent, the trend has changed."
        )
    if "Mixed Internals" in status:
        parts = []
        if pct_50 >= 60:
            parts.append(f"{pct_50:.0f}% of stocks above their 50-day MA (broad uptrend)")
        elif pct_50 >= 50:
            parts.append(f"{pct_50:.0f}% of stocks above their 50-day MA (mediocre — needs 60%+ for broad)")
        else:
            parts.append(f"only {pct_50:.0f}% of stocks above their 50-day MA (weakening)")
        if udr >= 1.3:
            parts.append(f"Up/Down volume {udr:.2f} (clear net buying)")
        elif udr >= 1.0:
            parts.append(f"Up/Down volume {udr:.2f} (barely positive — not real accumulation)")
        else:
            parts.append(f"Up/Down volume {udr:.2f} (mild net selling)")
        parts.append(f"{nh} true new highs vs {nl} true new lows")
        return (
            "Bullish and bearish signals are offsetting — "
            + " · ".join(parts)
            + ". Wait for one side to confirm before sizing up."
        )
    if "Accumulation" in status:
        return (
            f"Strong breadth ({pct_50:.0f}% of stocks above 50MA), Up/Down volume "
            f"{udr:.2f}, and {nh} new highs vs {nl} new lows = institutions buying "
            "broadly with leaders confirming. Most favourable backdrop for new positions."
        )
    return ""


def _classify_tier(c: dict) -> str:
    """Assign each candidate to A / B / C tier using Minervini-style criteria.

    A-Tier (TRADE NOW) — strict; typically 5-15 names:
      - RS >= 90 (top decile leadership)
      - VCP grade A or B (clean base structure)
      - Within 5% of pivot (actionable today)
      - NOT extended (<20% above 50MA — room to run)
      - Strong fundamentals (EPS+ or Sale+)
      - Top-15 industry rank (leaders from leading groups)
      - No earnings within 14 days (no binary catalyst risk)

    B-Tier (WATCH LIST) — looser; typically 30-60 names:
      - RS >= 85
      - VCP grade A, B, or C (any meaningful contraction)
      - Within 10% of pivot
      - <25% above 50MA
      - Has fundamentals OR RS line at new high
      - No earnings within 14 days

    C-Tier — everything else that passed the basic SEPA screen.
    """
    rs = c.get("RS", 0) or 0
    price = c.get("Price", 0) or 0
    buy = c.get("Buy_Trigger", 0) or 0
    vcp = c.get("VCP_Quality") or "None"
    pct50 = c.get("Pct_Above_50MA", 0) or 0
    fund = c.get("Fundamentals", "") or ""
    ind_rank = c.get("Industry_Rank")
    er_days = c.get("Days_To_Earnings")
    rs_nh = c.get("RS_NH")

    # Distance from current price to pivot (as % of price)
    dist_pct = (abs(buy - price) / price * 100) if price > 0 and buy > 0 else 999.0

    has_fund = ("EPS+" in fund) or ("Sale+" in fund)
    # Earnings is "safe" if already past, more than 14 days out, or unknown
    er_safe = er_days is None or er_days < 0 or er_days > 14

    # ---- A-Tier (Trade Now) — strict ----
    if (
        rs >= 90
        and vcp in ("A", "B")
        and dist_pct <= 5
        and pct50 < 20
        and has_fund
        and ind_rank is not None and ind_rank <= 15
        and er_safe
    ):
        return "A"

    # ---- B-Tier (Watch List) — looser ----
    if (
        rs >= 85
        and vcp in ("A", "B", "C")
        and dist_pct <= 10
        and pct50 < 25
        and (has_fund or rs_nh == "YES")
        and er_safe
    ):
        return "B"

    return "C"


def _candidates_dataframe(candidates: list[dict]) -> pd.DataFrame:
    """Normalize candidates into a display dataframe.

    Handles two schemas transparently:
      - new: separate `Sector` and `Industry` columns
      - legacy: combined `Sector_Industry` column (split on " | ")
    """
    if not candidates:
        return pd.DataFrame()
    df = pd.DataFrame(candidates)

    # Legacy cache compatibility: split Sector_Industry into two columns.
    # IMPORTANT: regex=False so the literal pipe isn't interpreted as alternation.
    if "Sector_Industry" in df.columns and "Sector" not in df.columns:
        splits = df["Sector_Industry"].astype(str).str.split(" | ", n=1, expand=True, regex=False)
        df["Sector"] = splits[0].astype(str).str.strip()
        df["Industry"] = (splits[1].fillna("") if 1 in splits.columns else "").astype(str).str.strip()
        df = df.drop(columns=["Sector_Industry"])

    # Defensive cleanup: strip any stray leading/trailing pipes + whitespace
    for col in ("Sector", "Industry"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lstrip("|").str.rstrip("|").str.strip()

    # ---- Backwards-compat for new v2 fields ----
    # Render Signals/Warnings lists into compact display strings ("BO TODAY · RS✓").
    if "Signals" in df.columns:
        df["Signals_Display"] = df["Signals"].apply(
            lambda x: " · ".join(x) if isinstance(x, list) else str(x or "")
        )
    else:
        df["Signals_Display"] = ""
    if "Warnings" in df.columns:
        df["Warnings_Display"] = df["Warnings"].apply(
            lambda x: " · ".join(x) if isinstance(x, list) else str(x or "")
        )
    else:
        df["Warnings_Display"] = ""

    # Backfill v1 columns that don't exist in v2 (so old cache files still render)
    if "VCP_Quality" not in df.columns and "VCP" in df.columns:
        df["VCP_Quality"] = df["VCP"]

    # Display column order — most important info first.
    # Shares + ATR + Stop_Reason + Power_Play + Breakout_Today are kept in the
    # underlying data (used by deep dive) but excluded from the table.
    # Compute Tier for every row (A/B/C) using strict Minervini criteria.
    # Done here on the raw candidate dicts before display ordering.
    df["Tier"] = [_classify_tier(c) for c in candidates]

    # RS_Leading was here but moved to EARLY mode 2026-06-07 — by construction,
    # almost no Momentum candidate is RS-leading-its-price (they're already in
    # Stage 2 near new highs). The signal lives where it can actually fire:
    # in the EARLY scan against the full universe.
    column_order = [
        "Ticker", "Tier", "RS", "RSI", "PEG", "Signals_Display", "Warnings_Display",
        "RS_NH", "VCP_Quality", "VDU", "Pressure_20d", "Fundamentals",
        "Price", "Buy_Trigger", "Stop", "Risk_Pct",
        "Pct_Above_50MA", "Vol_vs_Avg", "Days_To_Earnings", "Earnings_Date",
        "Index", "Industry_Rank", "Sector", "Market_Cap", "Industry",
    ]
    df = df[[c for c in column_order if c in df.columns]]
    return df


def _render_results_table(scan: dict):
    candidates = scan.get("candidates", [])
    df = _candidates_dataframe(candidates)
    if df.empty:
        st.info("No candidates passed the screen. The market may be in a bad regime, or settings are too strict.")
        return

    # Initialize toggle state for the 6 filter buttons (persists across reruns)
    for _k in ("flt_trade_now", "flt_watch_list", "flt_vcp_ab", "flt_vcp_b",
                "flt_breakout", "flt_buy_zone"):
        if _k not in st.session_state:
            st.session_state[_k] = False

    # Pre-compute the count each filter WOULD return on its own. We show this
    # count appended to the button label when the toggle is active, replacing
    # the previous checkmark. Counts are computed from the unfiltered scan so
    # they reflect the entire result set, not the result of stacking filters.
    def _count(mask) -> int:
        try:
            return int(mask.sum())
        except Exception:
            return 0
    n_trade_now = _count(df["Tier"] == "A") if "Tier" in df.columns else 0
    n_watch_list = _count(df["Tier"] == "B") if "Tier" in df.columns else 0
    n_vcp_ab = (
        _count(df["VCP_Quality"] == "A")
        if "VCP_Quality" in df.columns else 0
    )
    n_vcp_b = (
        _count(df["VCP_Quality"] == "B")
        if "VCP_Quality" in df.columns else 0
    )
    n_breakout = (
        _count(df["Signals_Display"].astype(str).str.contains("BO TODAY", na=False))
        if "Signals_Display" in df.columns else 0
    )
    n_buy_zone = (
        _count(df["Signals_Display"].astype(str).str.contains("BUY ZONE", na=False))
        if "Signals_Display" in df.columns else 0
    )

    def _btn_label(base: str, active: bool, count: int) -> str:
        # When active, append "(N)" instead of a checkmark. When inactive,
        # show just the base label so the row stays clean.
        return f"{base} ({count})" if active else base

    # Spacer that pushes the buttons down to vertically align with the dropdown
    # and text input (which have labels above them taking ~1.6rem of height)
    _BTN_SPACER = "<div style='height:1.65rem'></div>"

    # Filters row — Index | Industry | 6 toggle buttons | Search.
    # Industry filter added 2026-06-07: filter to stocks in top-N industry
    # groups by RS rank. O'Neil's research: ~49% of a stock's price move is
    # attributable to its industry group, so filtering to top industries is
    # the cleanest single-knob quality bump available.
    # VCP B Only added 2026-06-11 — A-grade is rare; B-grade is the practical
    # daily watchlist filter.
    # BUY ZONE added 2026-06-30 — Minervini Triple Barrel composite (Pressure +
    # near-50MA + VCP). Anticipatory pre-breakout entry filter.
    fcol1, fcol_ind, fcol2, fcol3, fcol4, fcol_b, fcol5, fcol_bz, fcol6 = st.columns(
        [1.5, 1.5, 1.2, 1.2, 1.1, 1.1, 1.2, 1.2, 1.6]
    )
    with fcol1:
        idx_filter = st.multiselect("Index", options=sorted(df["Index"].unique()), default=None, placeholder="All")
    with fcol_ind:
        # Industry filter — preset dropdown matching the INDEX widget pattern
        industry_choice = st.selectbox(
            "Industry rank",
            options=["All industries", "Top 30", "Top 15", "Top 10", "Top 5"],
            index=0,
            key="momentum_industry_filter",
            help="Filter to stocks whose industry group ranks in the top N by RS. "
                 "O'Neil's research: ~49% of a stock's price move is attributable "
                 "to its industry group. Filtering to top-N industries is the "
                 "cleanest one-knob quality bump available.",
        )
        # Map dropdown selection to a max rank threshold (None = no filter)
        _ind_rank_map = {
            "All industries": None,
            "Top 30": 30,
            "Top 15": 15,
            "Top 10": 10,
            "Top 5":  5,
        }
        max_industry_rank = _ind_rank_map.get(industry_choice)
    with fcol2:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("TRADE NOW", st.session_state.flt_trade_now, n_trade_now)
        if st.button(label, type="primary", use_container_width=True, key="btn_trade_now",
                     help="A-Tier only — strict Minervini criteria: RS≥90, VCP A/B, within 5% of pivot, <20% above 50MA, strong fundamentals, top-15 industry rank, no earnings within 14 days. Typically 5-15 names."):
            st.session_state.flt_trade_now = not st.session_state.flt_trade_now
            st.rerun()
        trade_now = st.session_state.flt_trade_now
    with fcol3:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("WATCH LIST", st.session_state.flt_watch_list, n_watch_list)
        if st.button(label, type="primary", use_container_width=True, key="btn_watch_list",
                     help="B-Tier only — looser: RS≥85, VCP A/B/C, within 10% of pivot, <25% above 50MA, fundamentals or RS new high, no earnings within 14 days. Setups developing — monitor for trigger."):
            st.session_state.flt_watch_list = not st.session_state.flt_watch_list
            st.rerun()
        watch_list = st.session_state.flt_watch_list
    with fcol4:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("VCP A ONLY", st.session_state.flt_vcp_ab, n_vcp_ab)
        if st.button(label, type="primary", use_container_width=True, key="btn_vcp_ab",
                     help="Only show grade-A VCP setups — the cleanest, textbook Minervini contraction pattern (3+ contractions, monotonically tightening)."):
            st.session_state.flt_vcp_ab = not st.session_state.flt_vcp_ab
            st.rerun()
        vcp_a_only = st.session_state.flt_vcp_ab
    with fcol_b:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("VCP B ONLY", st.session_state.flt_vcp_b, n_vcp_b)
        if st.button(label, type="primary", use_container_width=True, key="btn_vcp_b",
                     help="Only show grade-B VCP setups — meaningful multi-contraction structure that didn't quite meet every A criterion. The practical daily watchlist filter when A-grade is empty."):
            st.session_state.flt_vcp_b = not st.session_state.flt_vcp_b
            st.rerun()
        vcp_b_only = st.session_state.flt_vcp_b
    with fcol5:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("BREAKOUT", st.session_state.flt_breakout, n_breakout)
        if st.button(label, type="primary", use_container_width=True, key="btn_breakout",
                     help="Stocks that broke out above pivot today on volume"):
            st.session_state.flt_breakout = not st.session_state.flt_breakout
            st.rerun()
        breakout_only = st.session_state.flt_breakout
    with fcol_bz:
        st.markdown(_BTN_SPACER, unsafe_allow_html=True)
        label = _btn_label("BUY ZONE", st.session_state.flt_buy_zone, n_buy_zone)
        if st.button(label, type="primary", use_container_width=True, key="btn_buy_zone",
                     help="Minervini Triple Barrel composite — accumulation building "
                          "(Pressure 20d ≥1.3) while still near 50MA (-3% to +10%) "
                          "with a VCP pattern forming. Anticipatory pre-breakout "
                          "entry window — buy near support with tight stop, before "
                          "the obvious breakout when you'd be extended."):
            st.session_state.flt_buy_zone = not st.session_state.flt_buy_zone
            st.rerun()
        buy_zone_only = st.session_state.flt_buy_zone
    with fcol6:
        search = st.text_input("Search", placeholder="e.g. NVDA or tech")

    filtered = df.copy()
    if idx_filter:
        filtered = filtered[filtered["Index"].isin(idx_filter)]
    # Industry rank filter — only show stocks whose industry ranks <= max.
    # None Industry_Rank values are kept (graceful: don't drop stocks just
    # because their industry rank wasn't computed).
    if max_industry_rank is not None and "Industry_Rank" in filtered.columns:
        ranks = pd.to_numeric(filtered["Industry_Rank"], errors="coerce")
        filtered = filtered[ranks.isna() | (ranks <= max_industry_rank)]
    # VCP filter — A only, B only, or A+B if both toggles are on
    if (vcp_a_only or vcp_b_only) and "VCP_Quality" in filtered.columns:
        accepted_vcp = []
        if vcp_a_only: accepted_vcp.append("A")
        if vcp_b_only: accepted_vcp.append("B")
        filtered = filtered[filtered["VCP_Quality"].isin(accepted_vcp)]
    if breakout_only and "Signals_Display" in filtered.columns:
        filtered = filtered[filtered["Signals_Display"].astype(str).str.contains("BO TODAY", na=False)]
    if buy_zone_only and "Signals_Display" in filtered.columns:
        filtered = filtered[filtered["Signals_Display"].astype(str).str.contains("BUY ZONE", na=False)]
    # Tier filters — if both Trade Now AND Watch List are checked, show A+B
    if (trade_now or watch_list) and "Tier" in filtered.columns:
        accepted_tiers = []
        if trade_now: accepted_tiers.append("A")
        if watch_list: accepted_tiers.append("B")
        filtered = filtered[filtered["Tier"].isin(accepted_tiers)]
    if search:
        s = search.upper()
        mask = filtered["Ticker"].astype(str).str.upper().str.contains(s, na=False)
        if "Sector" in filtered.columns:
            mask = mask | filtered["Sector"].astype(str).str.upper().str.contains(s, na=False)
        if "Industry" in filtered.columns:
            mask = mask | filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)
        filtered = filtered[mask]

    # Default sort: RS descending. Users can click any column header in the
    # native dataframe to override.
    if "RS" in filtered.columns:
        try:
            filtered = filtered.sort_values("RS", ascending=False, na_position="last")
        except Exception:
            pass

    # Stash filtered df so the sidebar download buttons can read it
    st.session_state.filtered_candidates = filtered

    # Display the dataframe with row selection.
    # Ticker column is PINNED so it stays visible during horizontal scroll.
    column_config = {
        "Ticker": st.column_config.TextColumn("TICKER", width="small", pinned=True),
        "Tier": st.column_config.TextColumn(
            "TIER", width="small",
            help="A = Trade Now (strict Minervini criteria) · B = Watch List (looser, setups developing) · C = qualifying but not focus-list worthy",
        ),
        "RS": st.column_config.ProgressColumn("RS", min_value=0, max_value=99, format="%d", width="small"),
        "RSI": st.column_config.NumberColumn("RSI", format="%.1f", width="small"),
        "PEG": st.column_config.NumberColumn(
            "PEG", format="%.2f", width="small",
            help="Price/Earnings to Growth. <1 = growth is undervalued · 1-2 = fair · >2 = expensive growth. From yfinance .info; may be — for names without growth estimates.",
        ),
        "Signals_Display": st.column_config.TextColumn(
            "🟢 SIGNALS", width="medium",
            help="BO TODAY = breakout on volume · POWER PLAY = 100%+ in 8wk + consolidating · RS✓ = RS line new high · VCP-A/B = clean contractions",
        ),
        "Warnings_Display": st.column_config.TextColumn(
            "🔴 WARNINGS", width="medium",
            help="EXT = >20% above 50MA · ER Nd = earnings within N days · WIDE = stop > 7% · SQUAT = bearish reversal candle today",
        ),
        "RS_NH": st.column_config.TextColumn("RS NH", width="small", help="RS line at new 50-day high"),
        "VCP_Quality": st.column_config.TextColumn(
            "VCP", width="small",
            help="A = 3+ contractions, decreasing depth, current <10% · B = 2+ contractions, current <15% · C = 1+ contraction · None = no contraction detected",
        ),
        "VDU": st.column_config.TextColumn("VDU", width="small", help="Volume dry-up: Extreme < 50% avg · Yes < avg · No > avg (No is fine on a breakout day!)"),
        "Pressure_20d": st.column_config.NumberColumn(
            "PRESSURE", format="%.2fx", width="small",
            help="20-day Pressure (Minervini accumulation gauge): sum of up-volume "
                 "÷ sum of down-volume over the last 20 sessions. >1.3 = strong "
                 "accumulation (a BUY ZONE prerequisite). 1.0-1.3 = neutral. "
                 "<1.0 = distribution.",
        ),
        "Fundamentals": st.column_config.TextColumn("FUND", width="small"),
        "Price": st.column_config.NumberColumn("PRICE", format="$%.2f", width="small"),
        "Buy_Trigger": st.column_config.NumberColumn("BUY >", format="$%.2f", width="small"),
        "Stop": st.column_config.NumberColumn("STOP <", format="$%.2f", width="small"),
        "Risk_Pct": st.column_config.NumberColumn("RISK %", format="%.1f%%", width="small"),
        "Pct_Above_50MA": st.column_config.NumberColumn(
            "% > 50MA", format="%.1f%%", width="small",
            help="Distance above 50-day MA. >20% is extended (EXT warning fires).",
        ),
        "Vol_vs_Avg": st.column_config.NumberColumn(
            "VOL ×", format="%.1fx", width="small",
            help="Today's volume as a multiple of 50-day average. >1.5x on a breakout day = institutional buying.",
        ),
        "Days_To_Earnings": st.column_config.NumberColumn(
            "ER (d)", format="%d", width="small",
            help="Days until next earnings (negative if past). Avoid initiating swings within ~7 days of earnings.",
        ),
        "Earnings_Date": st.column_config.TextColumn(
            "ER DATE", width="small",
            help="Next scheduled earnings release date (YYYY-MM-DD)",
        ),
        "Index": st.column_config.TextColumn("INDEX", width="small"),
        "Industry_Rank": st.column_config.NumberColumn(
            "IND #", format="%d", width="small",
            help="Industry group rank by mean RS (1 = strongest). Minervini emphasizes leaders from leading groups.",
        ),
        "Market_Cap": st.column_config.TextColumn("MKT CAP", width="small"),
        "Sector": st.column_config.TextColumn("SECTOR", width="small"),
        "Industry": st.column_config.TextColumn("INDUSTRY"),
    }

    selection = st.dataframe(
        filtered,
        use_container_width=True,
        height=600,
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
    )

    # Handle selection -> deep dive
    if selection and "selection" in selection and selection["selection"].get("rows"):
        selected_row_idx = selection["selection"]["rows"][0]
        ticker = filtered.iloc[selected_row_idx]["Ticker"]
        st.session_state.deep_dive_ticker = ticker
        st.session_state.view = "deepdive"
        st.rerun()


def _render_diagnostics(scan: dict):
    diag = scan.get("diagnostics", {})
    if not diag:
        return

    # ---- Source status warning banner (loud if Wikipedia fetch failed) ----
    source_status = diag.get("source_status", {})
    if source_status:
        any_fallback = any(s.get("fallback_used") for s in source_status.values())
        any_failed = any(not s.get("ok", True) for s in source_status.values())
        if any_fallback or any_failed:
            problem_lines = []
            for name, st_dict in source_status.items():
                if st_dict.get("fallback_used") or not st_dict.get("ok"):
                    err = st_dict.get("error", "")
                    problem_lines.append(
                        f"&bull; <b>{name}:</b> {st_dict.get('count', 0)} tickers loaded — "
                        f"<span style='color:{PALETTE['signal_yellow']}'>using fallback list</span>"
                        + (f" ({err})" if err else "")
                    )
            st.markdown(
                f"<div class='signal-banner signal-banner-yellow' style='padding:0.75rem 1rem;font-size:0.85rem'>"
                f"<b>⚠ Index-membership source warning</b> — Some ticker sources had issues. "
                f"Stocks were still scanned (universe is healthy), but their INDEX column may default to "
                f"<b>RUT</b> if our fallback list doesn't cover them.<br><br>"
                + "<br>".join(problem_lines)
                + f"</div>",
                unsafe_allow_html=True,
            )

    with st.expander("📊 Filter elimination breakdown + ticker sources", expanded=False):
        cols = st.columns(4)
        labels = [
            ("Failed RS threshold", "failed_rs"),
            ("Failed Trend Template", "failed_trend"),
            ("Failed Fundamentals", "failed_fundamentals"),
            ("Other (data/calc)", "other"),
        ]
        for col, (label, key) in zip(cols, labels):
            col.metric(label, f"{diag.get(key, 0):,}")

        if source_status:
            st.markdown(
                f"<div class='panel-title' style='margin-top:1rem'>TICKER SOURCES (universe collection)</div>",
                unsafe_allow_html=True,
            )
            rows = []
            for name, info in source_status.items():
                icon = "✓" if info.get("ok") and not info.get("fallback_used") else "⚠"
                color = PALETTE["up_green"] if (info.get("ok") and not info.get("fallback_used")) else PALETTE["signal_yellow"]
                fb = " <i>(fallback)</i>" if info.get("fallback_used") else ""
                err = info.get("error") or ""
                rows.append(
                    f"<tr>"
                    f"<td style='padding:0.3rem 0.5rem;color:{color}'>{icon}</td>"
                    f"<td style='padding:0.3rem 0.5rem;font-weight:600'>{name}{fb}</td>"
                    f"<td style='padding:0.3rem 0.5rem;font-family:JetBrains Mono'>{info.get('count', 0):,}</td>"
                    f"<td style='padding:0.3rem 0.5rem;color:{PALETTE['text_muted']};font-size:0.85rem'>{err}</td>"
                    f"</tr>"
                )
            st.markdown(
                "<table style='width:100%;font-family:Inter;font-size:0.9rem'>"
                f"<thead><tr style='color:{PALETTE['text_muted']};text-align:left'>"
                "<th></th><th>Source</th><th>Tickers</th><th>Notes</th>"
                "</tr></thead><tbody>"
                + "".join(rows) +
                "</tbody></table>",
                unsafe_allow_html=True,
            )


def render_dashboard():
    _render_mode_switcher()
    scan = st.session_state.latest_scan

    st.markdown(
        f"<h1>💰 Legacy Brands Money Making Machine</h1>",
        unsafe_allow_html=True,
    )

    if scan is None:
        st.markdown(
            panel(
                "WELCOME",
                f"<div style='font-size:0.95rem;line-height:1.6'>"
                "No scan results yet. Click <b>▶ Start Scan</b> in the sidebar to run a full scan "
                "(~25 minutes), or <b>⚡ Demo</b> to test the pipeline on 20 well-known tickers (~2 min). "
                "<br><br>Once a scan completes, results are cached locally and load instantly on future opens. "
                "Click any row in the results table to dig into that stock with full charts, RS analysis, "
                "fundamentals, and a pre-computed trade execution ticket."
                "</div>"
            ),
            unsafe_allow_html=True,
        )
        return

    _render_market_strip(scan)
    _render_breadth_metrics(scan)
    _render_results_table(scan)
    _render_diagnostics(scan)

    # Footer note
    st.markdown(
        f"<div style='color:{PALETTE['text_muted']};font-size:0.75rem;text-align:center;margin-top:2rem;font-family:JetBrains Mono'>"
        f"Scan timestamp: {_format_timestamp(scan.get('timestamp', ''))} &nbsp;·&nbsp; "
        f"Mode: {scan.get('mode_label', '')} &nbsp;·&nbsp; "
        f"RS threshold: ≥{scan.get('rs_threshold', 0)}"
        "</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Deep dive view
# ---------------------------------------------------------------------------

def render_deep_dive_page():
    _render_mode_switcher()
    ticker = st.session_state.deep_dive_ticker

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{PALETTE['text_muted']};padding-top:0.5rem'>"
        f"DEEP DIVE  /  {ticker}"
        f"</div>",
        unsafe_allow_html=True,
    )

    settings: ScanSettings = st.session_state.settings
    render_deep_dive(ticker, settings.account_size, settings.risk_per_trade)


# ---------------------------------------------------------------------------
# Top Stocks view — RETIRED
# ---------------------------------------------------------------------------
# The old composite-scoring Top Stocks page (and its _score_candidate helper)
# was retired when Top Stocks was redesigned as the intersection of the
# Long + Quality scans. The new page lives in ui/top_stocks_view.py and is
# imported at the top of this file. See scanner/top_stocks.py for the data
# layer.


def render_diagnose_page():
    _render_mode_switcher()
    ticker = st.session_state.get("diagnose_ticker", "")

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{PALETTE['text_muted']};padding-top:0.5rem'>"
        f"DIAGNOSE  /  {ticker}"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(f"<h2>🔍 Diagnostic for {ticker}</h2>", unsafe_allow_html=True)
    st.caption("Why did (or didn\'t) this ticker pass the SEPA scan?")

    # Lazy import to avoid circular issues
    from scanner.diagnose import diagnose_ticker
    settings: ScanSettings = st.session_state.settings

    with st.spinner(f"Downloading {ticker} data and running tests…"):
        result = diagnose_ticker(ticker, settings)

    if result is None:
        st.error(f"Could not load data for {ticker}. Yahoo Finance may not have it, or the ticker is delisted.")
        return

    overall_color = PALETTE["up_green"] if result["passes_all"] else PALETTE["down_red"]
    overall_text = "PASSES SEPA SCREEN" if result["passes_all"] else "FAILS SEPA SCREEN"
    st.markdown(
        f"<div class='signal-banner' style='border:1px solid {overall_color};color:{overall_color};font-size:1.1rem'>"
        f"{overall_text}"
        f"</div>",
        unsafe_allow_html=True,
    )

    green = PALETTE["up_green"]
    red = PALETTE["down_red"]
    rows = []
    for test in result["tests"]:
        passed = test["passed"]
        icon = f"<span style='color:{green}'>OK</span>" if passed else f"<span style='color:{red}'>X</span>"
        rows.append(
            f"<tr><td style='padding:0.4rem 0.5rem'>{icon}</td>"
            f"<td style='padding:0.4rem 0.5rem;font-weight:600'>{test['name']}</td>"
            f"<td style='padding:0.4rem 0.5rem;color:{PALETTE['text_muted']}'>{test['detail']}</td></tr>"
        )
    body = (
        "<table style='width:100%;font-family:Inter;font-size:0.9rem'>"
        + "".join(rows) +
        "</table>"
    )
    st.markdown(panel("TEST RESULTS", body), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Table of Contents dialog
# ---------------------------------------------------------------------------

@st.dialog("Table of Contents - Column Definitions", width="large")
def _show_toc_dialog():
    """Modal dialog explaining every column, signal, and warning.

    Called DIRECTLY from a button (not via session_state flag) so the dialog
    only appears when the user clicks Table of Contents - never as a side
    effect of clicking Top Stocks, Diagnose, or any other button.
    """
    from ui.toc import COLUMN_DOCS, SIGNAL_DOCS_LONG, SIGNAL_DOCS_SHORT, WARNING_DOCS

    muted = PALETTE["text_muted"]
    cyan = PALETTE["accent_cyan"]
    green = PALETTE["up_green"]
    red = PALETTE["down_red"]

    st.markdown(
        f"<div style='color:{muted};font-size:0.85rem;margin-bottom:1rem'>"
        "Every column in the candidates table, with definitions and what each value means."
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(f"<h4 style='margin-top:0;color:{cyan}'>COLUMNS</h4>", unsafe_allow_html=True)
    for col, doc in COLUMN_DOCS.items():
        with st.expander(f"-- {col}", expanded=False):
            st.markdown(doc)

    st.markdown(
        f"<h4 style='color:{green};margin-top:1.5rem'>LONG-SIDE GREEN SIGNALS (bullish)</h4>",
        unsafe_allow_html=True,
    )
    with st.expander("-- All positive signal tags", expanded=False):
        for tag, doc in SIGNAL_DOCS_LONG.items():
            st.markdown(
                f"<div style='margin-bottom:0.75rem'>"
                f"<b style='font-family:JetBrains Mono;color:{green};font-size:1rem'>{tag}</b>"
                f"<div style='margin-top:0.25rem'>{doc}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown(
        f"<h4 style='color:{red};margin-top:1.5rem'>RED WARNINGS (caution)</h4>",
        unsafe_allow_html=True,
    )
    with st.expander("-- All caution warning tags", expanded=False):
        for tag, doc in WARNING_DOCS.items():
            st.markdown(
                f"<div style='margin-bottom:0.75rem'>"
                f"<b style='font-family:JetBrains Mono;color:{red};font-size:1rem'>{tag}</b>"
                f"<div style='margin-top:0.25rem'>{doc}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    if st.button("Close", use_container_width=True):
        st.rerun()


# ---------------------------------------------------------------------------
# Research mode -- load deep-dive for any ticker without needing a scan
# ---------------------------------------------------------------------------

def _render_research_sidebar():
    """Lightweight sidebar for Research mode."""
    cyan = PALETTE['accent_cyan']
    muted = PALETTE['text_muted']
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{cyan};margin-bottom:0.5rem;line-height:1.2'>\U0001F50E LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;color:{cyan};margin-bottom:0.5rem'>RESEARCH MODE</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.5rem'>Deep-dive on any ticker -- no scan needed</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        panel("HOW IT WORKS",
              f"<div style='font-family:Inter;font-size:0.85rem;line-height:1.5;color:{muted}'>"
              f"Type any U.S.-listed ticker into the box and hit Enter. "
              f"You will get the same four tabs you see for scanned candidates: "
              f"<b style='color:{cyan}'>Fundamentals</b>, "
              f"<b style='color:{cyan}'>Execution</b>, "
              f"<b style='color:{cyan}'>Price &amp; VCP</b>, and "
              f"<b style='color:{cyan}'>RS Line</b>."
              f"</div>"),
        unsafe_allow_html=True,
    )
    st.markdown("---")
    if st.session_state.get("research_ticker"):
        if st.button("\U0001F504 New ticker", type="primary", use_container_width=True,
                     help="Clear the current ticker and search a different one"):
            st.session_state.research_ticker = None
            st.rerun()


def render_research_page():
    """Research view: ticker search box, then full deep-dive for that ticker."""
    _render_mode_switcher()

    cyan = PALETTE['accent_cyan']
    muted = PALETTE['text_muted']

    current = st.session_state.get("research_ticker")

    if not current:
        st.markdown(
            f"<div style='text-align:center;margin-top:2.5rem;margin-bottom:1.5rem'>"
            f"<div style='font-family:JetBrains Mono;font-size:1.8rem;font-weight:700;color:{cyan};letter-spacing:0.05em'>\U0001F50E RESEARCH ANY TICKER</div>"
            f"<div style='font-family:Inter;font-size:0.95rem;color:{muted};margin-top:0.4rem'>Type a symbol and hit Enter to load Fundamentals, Execution, Price &amp; VCP, and RS Line.</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        col_l, col_c, col_r = st.columns([2, 3, 2])
        with col_c:
            with st.form("research_ticker_form", clear_on_submit=False):
                ticker_input = st.text_input(
                    "Ticker symbol",
                    placeholder="e.g. NVDA",
                    key="research_ticker_input",
                    label_visibility="visible",
                )
                submitted = st.form_submit_button("\U0001F50E Research", type="primary", use_container_width=True)
                if submitted and ticker_input.strip():
                    st.session_state.research_ticker = ticker_input.strip().upper()
                    st.rerun()
        return

    if st.button("← New search", type="primary", key="research_back"):
        st.session_state.research_ticker = None
        st.rerun()
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;color:{muted};letter-spacing:0.1em;margin-bottom:0.5rem'>RESEARCH / {current}</div>",
        unsafe_allow_html=True,
    )

    settings: ScanSettings = st.session_state.settings
    render_deep_dive(current, settings.account_size, settings.risk_per_trade)


# ---------------------------------------------------------------------------
# Twitter / Social Momentum view
# ---------------------------------------------------------------------------

@st.dialog("Social Scan - Table of Contents", width="large")
def _show_social_toc_dialog():
    """TOC for the Twitter mode. Documents social-specific columns plus the
    technical/fundamental columns that come from the standard enrichment.
    Skeleton entry — fleshed out in Phase 5."""
    from ui.toc import COLUMN_DOCS
    muted = PALETTE["text_muted"]
    cyan = PALETTE["accent_cyan"]
    st.markdown(
        f"<div style='color:{muted};font-size:0.85rem;margin-bottom:1rem'>"
        "Social-scan columns. The social columns (mentions, spike ratios, "
        "sentiment) drive WHICH tickers appear; the technical/fundamental "
        "columns help you decide WHICH of those tickers are worth a chart load."
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<h4 style='margin-top:0;color:{cyan}'>SOCIAL COLUMNS</h4>", unsafe_allow_html=True)
    social_docs = {
        "Mentions (24h)": "Number of posts mentioning this ticker on Apewisdom-tracked sources (WSB, stocks, investing, pennystocks, options, Twitter) over the last 24 hours.",
        "Spike 24h": "Today's mention count divided by yesterday's. Values > 1.5 indicate a meaningful surge; 3+ is a strong spike.",
        "Spike 3d / 7d / 30d": "Today's mentions vs the average over the trailing window. Requires snapshot history (populates after the first few days of use).",
        "Sentiment %": "Apewisdom's aggregated bullish-share score (0-100). Bullish ≥65, Neutral 40-64, Bearish <40.",
        "Rank Δ": "Change in social ranking vs yesterday. Positive = climbing the leaderboard. Big positive jumps + high spike = real momentum.",
    }
    for label, doc in social_docs.items():
        with st.expander(f"-- {label}", expanded=False):
            st.markdown(f"**{label}.** {doc}")

    st.markdown(f"<h4 style='margin-top:1.5rem;color:{cyan}'>TECHNICAL / FUNDAMENTAL COLUMNS</h4>", unsafe_allow_html=True)
    # Reuse a subset of the long-side column docs
    technical_keys = [
        "RS", "RSI", "% > 50MA", "VCP", "🟢 SIGNALS", "🔴 WARNINGS",
        "PEG", "FUND", "MKT CAP", "SECTOR", "INDUSTRY",
    ]
    for key in technical_keys:
        doc = COLUMN_DOCS.get(key)
        if doc:
            with st.expander(f"-- {key}", expanded=False):
                st.markdown(doc)

    if st.button("Close", use_container_width=True, key="social_toc_close"):
        st.rerun()


def _social_candidates_dataframe(candidates: list) -> "pd.DataFrame":
    """Turn the enriched candidate list into a display DataFrame."""
    import pandas as pd
    if not candidates:
        return pd.DataFrame()
    df = pd.DataFrame(candidates)

    # Build Signals/Warnings display strings (lists -> joined text)
    def _join_list(v):
        if isinstance(v, list):
            return " - ".join(str(x) for x in v) if v else ""
        return str(v) if v else ""
    if "Signals" in df.columns:
        df["Signals_Display"] = df["Signals"].apply(_join_list)
    if "Warnings" in df.columns:
        df["Warnings_Display"] = df["Warnings"].apply(_join_list)

    # Pick + order the columns we actually display
    column_order = [
        "Ticker", "Mentions",
        "Spike_24h", "Spike_3d", "Spike_7d", "Spike_30d",
        "Sentiment_Score", "Sentiment_Label", "Rank_Delta",
        "Price", "RS", "RSI", "Pct_Above_50MA",
        "VCP_Quality", "Signals_Display", "Warnings_Display",
        "PEG", "Held_Pct_Inst", "Fundamentals",
        "Sector", "Market_Cap", "Industry",
    ]
    cols_present = [c for c in column_order if c in df.columns]
    return df[cols_present]


def render_twitter_page():
    """Social momentum dashboard — Apewisdom-sourced trending tickers,
    enriched with the same technical/fundamental data as the momentum scan.
    Click any row to deep-dive (includes the new SOCIAL tab in Phase 4)."""
    _render_mode_switcher()

    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.5rem;font-weight:700;color:{cyan};margin-bottom:0.4rem'>"
        "🐦 SOCIAL MOMENTUM</div>"
        f"<div style='color:{muted};font-size:0.9rem;line-height:1.5;margin-bottom:1rem'>"
        "Top trending tickers from Apewisdom (Reddit + Twitter sources), cross-referenced "
        "with your tradeable universe (price ≥ $2.50, US-listed, avg vol ≥ 100K). "
        "Each row carries the same technical + fundamental enrichment as the momentum scan, so you "
        "can filter spikes down to tradeable setups before loading any chart."
        "</div>",
        unsafe_allow_html=True,
    )

    scan = st.session_state.get("latest_social_scan")
    if not scan:
        st.info(
            "No social scan yet. Click **🔄 Refresh** in the sidebar to fetch the current "
            "trending tickers. Takes ~30-60 seconds with warm caches."
        )
        return

    candidates = scan.get("candidates", [])
    if not candidates:
        st.warning(
            "Latest social scan returned no tickers passing the guardrails. "
            "Try lowering the price minimum or check Apewisdom availability."
        )
        diagnostics = scan.get("diagnostics", {})
        if diagnostics:
            with st.expander("Scan diagnostics"):
                st.json(diagnostics)
        return

    df = _social_candidates_dataframe(candidates)
    if df.empty:
        st.info("No candidates to display.")
        return

    # ---- Filter row ----
    # Spacer to vertically align buttons with widget labels (same trick as longs)
    _BTN_SPACER = "<div style='height:1.65rem'></div>"

    fcol1, fcol2, fcol3, fcol4 = st.columns([1.5, 1.5, 1.5, 3])
    with fcol1:
        min_mentions = st.slider(
            "Min mentions (24h)",
            min_value=0, max_value=500, value=25, step=5,
            help="Drop tickers with fewer than this many mentions in the last 24h. Filters noise.",
        )
    with fcol2:
        min_spike = st.slider(
            "Min 24h spike",
            min_value=0.0, max_value=10.0, value=1.5, step=0.1,
            help="Today's mentions ÷ yesterday's. 1.5× = mild surge, 3× = strong spike. "
                 "Tickers with no 24h ratio yet (snapshot history gap) always pass through. "
                 "Slide to 0 to also include tickers whose mention count DROPPED from yesterday.",
        )
    with fcol3:
        sentiment_filter = st.selectbox(
            "Sentiment",
            options=["All", "Bullish", "Neutral", "Bearish"],
            index=0,
            help="Bullish ≥65%, Bearish <40%, Neutral in between (Apewisdom sentiment score).",
        )
    with fcol4:
        search = st.text_input(
            "Search",
            placeholder="e.g. NVDA or tech",
            help="Filter by ticker, sector, or industry.",
        )

    # ---- Apply filters ----
    filtered = df.copy()
    if "Mentions" in filtered.columns:
        filtered = filtered[filtered["Mentions"] >= min_mentions]
    if "Spike_24h" in filtered.columns:
        # Tickers with NaN/None Spike_24h pass through (cold-history day 1)
        mask = filtered["Spike_24h"].isna() | (filtered["Spike_24h"] >= min_spike)
        filtered = filtered[mask]
    if sentiment_filter != "All" and "Sentiment_Label" in filtered.columns:
        filtered = filtered[filtered["Sentiment_Label"] == sentiment_filter]
    if search:
        s = search.upper()
        mask = filtered["Ticker"].astype(str).str.upper().str.contains(s, na=False)
        if "Sector" in filtered.columns:
            mask |= filtered["Sector"].astype(str).str.upper().str.contains(s, na=False)
        if "Industry" in filtered.columns:
            mask |= filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)
        filtered = filtered[mask]

    # Default sort: Spike_24h descending, then Mentions descending
    sort_cols = []
    if "Spike_24h" in filtered.columns:
        sort_cols.append("Spike_24h")
    if "Mentions" in filtered.columns:
        sort_cols.append("Mentions")
    if sort_cols:
        try:
            filtered = filtered.sort_values(sort_cols, ascending=False, na_position="last")
        except Exception:
            pass

    # Stash for sidebar downloads
    st.session_state.filtered_social_candidates = filtered

    # ---- Column config ----
    column_config = {
        "Ticker": st.column_config.TextColumn("TICKER", width="small", pinned=True),
        "Mentions": st.column_config.NumberColumn("MENTIONS", format="%d", width="small",
            help="Apewisdom mention count over the last 24 hours"),
        "Spike_24h": st.column_config.NumberColumn("SPIKE 24h", format="%.2fx", width="small",
            help="Today's mentions vs yesterday's. Empty = no snapshot history yet."),
        "Spike_3d": st.column_config.NumberColumn("SPIKE 3d", format="%.2fx", width="small",
            help="Today vs avg of last 3 days. Requires history."),
        "Spike_7d": st.column_config.NumberColumn("SPIKE 7d", format="%.2fx", width="small",
            help="Today vs avg of last 7 days. Requires history."),
        "Spike_30d": st.column_config.NumberColumn("SPIKE 30d", format="%.2fx", width="small",
            help="Today vs avg of last 30 days. Requires history."),
        "Sentiment_Score": st.column_config.NumberColumn("SENT %", format="%.0f", width="small",
            help="Apewisdom bullish-share score, 0-100"),
        "Sentiment_Label": st.column_config.TextColumn("LABEL", width="small",
            help="Bullish ≥65, Neutral 40-64, Bearish <40"),
        "Rank_Delta": st.column_config.NumberColumn("RANK Δ", format="%+d", width="small",
            help="Climb in social rank vs yesterday (positive = climbing)"),
        "Price": st.column_config.NumberColumn("PRICE", format="$%.2f", width="small"),
        "RS": st.column_config.NumberColumn("RS", format="%d", width="small",
            help="Universe-relative RS rating (0-99) from latest momentum scan. Empty = no momentum scan available or ticker not in momentum scan."),
        "RSI": st.column_config.NumberColumn("RSI", format="%.0f", width="small",
            help="14-period RSI; >70 overbought, <30 oversold"),
        "Pct_Above_50MA": st.column_config.NumberColumn("% > 50MA", format="%.1f%%", width="small",
            help=">20% triggers EXT warning (extended)"),
        "VCP_Quality": st.column_config.TextColumn("VCP", width="small",
            help="A = textbook Minervini base, B = good, C = mediocre, None = no clear contraction pattern"),
        "Signals_Display": st.column_config.TextColumn("🟢 SIGNALS", width="medium",
            help="BO TODAY = breakout on volume · POWER PLAY · VCP-A/B"),
        "Warnings_Display": st.column_config.TextColumn("🔴 WARNINGS", width="medium",
            help="EXT = extended >20% above 50MA · WIDE = stop >7% below pivot"),
        "PEG": st.column_config.NumberColumn("PEG", format="%.2f", width="small",
            help="Price/Earnings to Growth — <1 cheap growth, >2 expensive, >3 priced for perfection"),
        "Held_Pct_Inst": st.column_config.NumberColumn("INST %", format="%.0f", width="small",
            help="% institutional ownership"),
        "Fundamentals": st.column_config.TextColumn("FUND", width="small",
            help="EPS++ = accelerating EPS growth, EPS+ = >25% YoY, Sale+ = >20% YoY revenue"),
        "Sector": st.column_config.TextColumn("SECTOR", width="small"),
        "Industry": st.column_config.TextColumn("INDUSTRY"),
        "Market_Cap": st.column_config.TextColumn("MKT CAP", width="small"),
    }

    st.caption(
        f"Showing {len(filtered)} of {len(df)} tickers · "
        f"sort SPIKE 24h desc · click a row to deep-dive"
    )

    selection = st.dataframe(
        filtered,
        use_container_width=True,
        height=600,
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
    )

    # Handle selection -> deep dive
    if selection and "selection" in selection and selection["selection"].get("rows"):
        selected_row_idx = selection["selection"]["rows"][0]
        ticker = filtered.iloc[selected_row_idx]["Ticker"]
        st.session_state.deep_dive_ticker = ticker
        st.session_state.view = "deepdive"
        st.rerun()

    # Scan diagnostics expander (small info dump at the bottom for debugging)
    diagnostics = scan.get("diagnostics", {})
    if diagnostics:
        with st.expander("Scan diagnostics", expanded=False):
            st.json(diagnostics)


# ---------------------------------------------------------------------------
# Rotation mode — sector / industry rotation + leaders drill-down
# ---------------------------------------------------------------------------

def _render_rotation_sidebar():
    """Rotation sidebar — branding + Refresh + Excel download + TOC.

    Refresh re-reads the latest cached scan and recomputes the rotation
    table. TOC opens the rotation-column glossary dialog. Excel downloads
    the current rotation table (after sector filter / search) — useful for
    capturing a snapshot to share or eyeball offline.
    """
    cyan = PALETTE['accent_cyan']
    muted = PALETTE['text_muted']
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;color:{cyan};margin-bottom:0.5rem;line-height:1.2'>🔄 LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;color:{cyan};margin-bottom:0.5rem'>ROTATION</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.25rem'>Where capital is flowing — sectors &amp; leaders</div>",
        unsafe_allow_html=True,
    )

    # ---- Refresh: re-reads latest cached long scan + rebuilds table ----
    if st.button("▶ Run Scan", type="primary", use_container_width=True,
                 key="rotation_refresh",
                 help="Re-read the latest cached momentum scan from disk and "
                      "rebuild the rotation table. Useful after running a "
                      "new momentum scan elsewhere."):
        # Reload the latest scan into session state, then rerun
        st.session_state.latest_scan = load_latest_scan()
        st.rerun()

    # ---- Excel download of the current rotation table ----
    rot_df = st.session_state.get("rotation_filtered_df")
    if rot_df is not None and len(rot_df) > 0:
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                rot_df,
                sheet_name="Rotation",
                title=f"Legacy Brands - Industry Rotation {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"legacy_brands_rotation_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")
    else:
        st.caption("No rotation table loaded yet.")

    # ---- Table of Contents ----
    if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                 key="rotation_toc_btn",
                 help="Definitions for every column on the Rotation page."):
        _show_rotation_toc_dialog()

    st.markdown("---")

    # ---- Helper panel: how it works ----
    st.markdown(
        panel("HOW IT WORKS",
              f"<div style='font-family:Inter;font-size:0.85rem;line-height:1.55;color:{muted}'>"
              f"Industries ranked by current RS, with <b style='color:{cyan}'>rank change vs 1 week &amp; 4 weeks ago</b> "
              f"showing actual rotation. Industries climbing the leaderboard fastest = where "
              f"money is moving NOW.<br><br>"
              f"<b style='color:{cyan}'>Click any industry row</b> to drill down to the leader "
              f"stocks within that group."
              f"</div>"),
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div class='panel-title'>HISTORY NOTE</div>"
        f"<div style='color:{muted};font-size:0.78rem;line-height:1.5;margin-top:0.4rem'>"
        f"Rotation uses your cached scans for history. The 1-week column populates after "
        f"~1 week of regular scans; the 4-week column after ~4 weeks. Older = better."
        f"</div>",
        unsafe_allow_html=True,
    )

    # If drilled into an industry (Layer 2), surface a clear back button
    selected = st.session_state.get("rotation_selected_industry")
    if selected:
        st.markdown("---")
        if st.button("← Back to Rotation", type="primary", use_container_width=True,
                     key="rotation_sidebar_back"):
            st.session_state.rotation_selected_industry = None
            st.rerun()


@st.dialog("Rotation — Table of Contents", width="large")
def _show_rotation_toc_dialog():
    """Brief column glossary for the rotation table."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    st.markdown(
        f"<div style='color:{muted};font-size:0.85rem;margin-bottom:1rem'>"
        "Columns in the industry rotation table."
        "</div>",
        unsafe_allow_html=True,
    )
    cols = [
        ("Industry", "GICS industry group. Must have at least 3 candidate stocks to be ranked."),
        ("Sector", "Parent GICS sector (Tech, Healthcare, etc.). Use the filter above the table to slice by sector."),
        ("Members", "How many stocks from your scan belong to this industry. More members = more reliable RS average."),
        ("Cur Rank", "Today's rank by mean RS within the industry (1 = strongest)."),
        ("Rank 1w / 4w", "Where the industry ranked ~1 week ago / ~4 weeks ago in your scan history. \"—\" means no scan from that period exists yet."),
        ("Δ1w / Δ4w", "Rank change. Positive = climbing the leaderboard (the rotation signal). Negative = falling out of favor."),
        ("Avg RS", "Mean RS rating across the industry's member stocks."),
        ("% > 50MA", "Pct of the industry's member stocks trading above their 50-day MA. Broader = more bullish."),
        ("Rotation Score", "Weighted blend of mean RS (baseline), 4w rank change (×1.5), and 1w rank change (×0.75). Default sort column."),
    ]
    for name, doc in cols:
        with st.expander(f"-- {name}", expanded=False):
            st.markdown(doc)
    if st.button("Close", use_container_width=True, key="rotation_toc_close"):
        st.rerun()


def _format_rank_delta_html(delta) -> str:
    """Render a rank delta as colored HTML: green ▲ for climbs, red ▼ for falls,
    gray dash for missing history."""
    muted = PALETTE["text_muted"]
    up = PALETTE["up_green"]
    down = PALETTE["down_red"]
    dash_html = f"<span style='color:{muted}'>—</span>"
    if delta is None:
        return dash_html
    try:
        d = int(delta)
    except Exception:
        return dash_html
    if d > 0:
        return f"<span style='color:{up};font-weight:700'>▲ +{d}</span>"
    if d < 0:
        return f"<span style='color:{down};font-weight:700'>▼ {d}</span>"
    return f"<span style='color:{muted}'>0</span>"


def _render_rotation_summary_strip(rot_df, current_industry=None):
    """Top-6 hottest industries as small clickable cards. Shows above the
    Layer 2 leaders table so the user keeps peripheral rotation context."""
    from scanner.rotation import top_rotators
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    primary = PALETTE["text_primary"]
    up = PALETTE["up_green"]
    down = PALETTE["down_red"]

    top = top_rotators(rot_df, n=6)
    if top.empty:
        return

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.72rem;color:{muted};"
        f"letter-spacing:0.08em;margin-bottom:0.35rem'>TOP ROTATORS — click to switch industry</div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(len(top))
    for i, (_, row) in enumerate(top.iterrows()):
        ind = row.get("Industry", "")
        rs = row.get("MeanRS")
        delta_4w = row.get("Delta_4w")
        delta_1w = row.get("Delta_1w")
        rank = row.get("Rank")
        # Prefer 4w delta if we have it; else fall back to 1w
        delta = delta_4w if (delta_4w is not None and not pd.isna(delta_4w)) else delta_1w
        is_current = (ind == current_industry)
        with cols[i]:
            border_color = cyan if is_current else PALETTE["border"]
            delta_color = up if (delta is not None and not pd.isna(delta) and delta > 0) else (
                down if (delta is not None and not pd.isna(delta) and delta < 0) else muted
            )
            delta_str = "—" if delta is None or pd.isna(delta) else (
                f"▲ +{int(delta)}" if delta > 0 else (f"▼ {int(delta)}" if delta < 0 else "0")
            )
            # Truncate long industry names so they fit one line
            short_ind = ind if len(ind) <= 22 else ind[:21] + "…"
            label = f"{short_ind}\n{delta_str}"
            if st.button(label, key=f"rot_strip_{i}_{ind}",
                         type=("primary" if is_current else "secondary"),
                         use_container_width=True,
                         help=f"{ind} · #{rank} · RS {rs:.0f} · {delta_str} ranks ({'4w' if delta_4w is not None and not pd.isna(delta_4w) else '1w'})"):
                if not is_current:
                    st.session_state.rotation_selected_industry = ind
                    st.rerun()


def render_rotation_page():
    """Main rotation view — Layer 1 (industry table) or Layer 2 (leaders within
    selected industry) based on session state."""
    import pandas as pd
    _render_mode_switcher()

    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    # Rotation now sources from Quality scans (broader universe, more history
    # available). The Momentum scan is still used for Layer 2 (industry-leader
    # drill-down) since those candidates have the actual SEPA setups.
    scan = st.session_state.latest_scan or {}

    # Compute the rotation table — internally prefers Quality scans, falls
    # back to Momentum if no Quality scans exist.
    from scanner.rotation import compute_rotation_table, list_quality_scans
    result = compute_rotation_table(scan)
    rot_df = result["df"]
    history = result["history"]
    rotation_source = result.get("source", "none")

    # If rotation couldn't compute at all, tell the user what's needed
    if rot_df.empty:
        if not list_quality_scans() and not (scan and scan.get("candidates")):
            st.warning(
                "Rotation analysis needs a scan to anchor on. Run a Quality scan "
                "(QUALITY tab → ▶ Run Scan) — that gives the rotation table the "
                "broadest, most statistically meaningful industry universe. "
                "Momentum scans also work as a fallback if you've run one."
            )
        else:
            st.warning(
                "Rotation couldn't compute industry rankings. The most recent scan "
                "may not have enough industry-tagged data."
            )
        return

    selected = st.session_state.get("rotation_selected_industry")

    # =====================================================================
    # LAYER 2 — drilled into a specific industry, show leaders within it
    # =====================================================================
    if selected:
        # (Top Rotators strip removed per user request.)

        # Back button + downloads + heading
        col_back, col_xlsx, col_txt, col_heading = st.columns([1.4, 1.4, 1.4, 5])
        with col_back:
            if st.button("← Back to Rotation", type="primary", use_container_width=True,
                         key="rotation_back_btn"):
                st.session_state.rotation_selected_industry = None
                st.rerun()
        # The actual download_button widgets need to come AFTER we compute the
        # filtered candidates df below; we render placeholders here and fill
        # them in via st.empty() once we have the data.
        xlsx_slot = col_xlsx.empty()
        txt_slot = col_txt.empty()
        with col_heading:
            # Look up the industry's stats for the heading
            row = rot_df[rot_df["Industry"] == selected]
            stats_html = ""
            if len(row):
                r = row.iloc[0]
                d1 = r.get("Delta_1w")
                d4 = r.get("Delta_4w")
                d1_str = _format_rank_delta_html(d1)
                d4_str = _format_rank_delta_html(d4)
                stats_html = (
                    f"<div style='font-family:JetBrains Mono;font-size:0.78rem;color:{muted};"
                    f"margin-top:0.4rem'>"
                    f"#{int(r['Rank'])} · {int(r['Members'])} members · "
                    f"Avg RS {r['MeanRS']:.0f} · "
                    f"% > 50MA {r['PctAbove50MA']:.0f}% · "
                    f"Δ1w {d1_str} · Δ4w {d4_str}"
                    f"</div>"
                )
            st.markdown(
                f"<div style='font-family:JetBrains Mono;font-size:1.5rem;font-weight:700;"
                f"color:{cyan};line-height:1.1'>{selected}</div>"
                f"{stats_html}",
                unsafe_allow_html=True,
            )

        # Filter the scan's candidates to this industry, render as a small inline table.
        cands = [c for c in scan.get("candidates", []) if (c.get("Industry") or "") == selected]
        if not cands:
            st.warning(f"No candidate stocks found for industry '{selected}'.")
            return

        df = _candidates_dataframe(cands)
        if df.empty:
            st.info("No data to display for this industry.")
            return

        # Sort by RS descending — that's "leaders" in this industry
        if "RS" in df.columns:
            df = df.sort_values("RS", ascending=False, na_position="last")

        # Fill the download-button placeholders we created up top
        try:
            stamp = datetime.utcnow().strftime("%d%b%Y")
            safe_ind = "".join(c for c in selected if c.isalnum() or c in "-_") or "industry"
            excel_bytes = format_dataframe_to_excel(
                df,
                sheet_name=selected[:31],
                title=f"Legacy Brands - {selected} {stamp}",
            )
            xlsx_slot.download_button(
                label="📥 Excel",
                data=excel_bytes,
                file_name=f"legacy_brands_{safe_ind}_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
                key=f"rot_l2_xlsx_{selected}",
            )
        except Exception as e:
            xlsx_slot.caption(f"Excel: {e}")
        try:
            tickers_text = "\n".join(df["Ticker"].astype(str).tolist())
            stamp = datetime.utcnow().strftime("%d%b%Y")
            txt_slot.download_button(
                label="📥 Tickers (.txt)",
                data=tickers_text,
                file_name=f"legacy_brands_{safe_ind}_tickers_{stamp}.txt",
                mime="text/plain",
                use_container_width=True,
                type="primary",
                key=f"rot_l2_txt_{selected}",
            )
        except Exception as e:
            txt_slot.caption(f"Tickers: {e}")

        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.8rem;color:{muted};"
            f"margin-top:0.7rem;margin-bottom:0.35rem'>"
            f"LEADERS IN {selected.upper()} ({len(df)} stocks) — sorted by RS desc · click any row to deep-dive"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Column config — reuse the same shape as the longs table
        column_config = {
            "Ticker": st.column_config.TextColumn("TICKER", width="small", pinned=True),
            "Tier": st.column_config.TextColumn("TIER", width="small"),
            "RS": st.column_config.ProgressColumn("RS", min_value=0, max_value=99, format="%d", width="small"),
            "RSI": st.column_config.NumberColumn("RSI", format="%.1f", width="small"),
            "PEG": st.column_config.NumberColumn("PEG", format="%.2f", width="small"),
            "Signals_Display": st.column_config.TextColumn("🟢 SIGNALS", width="medium"),
            "Warnings_Display": st.column_config.TextColumn("🔴 WARNINGS", width="medium"),
            "VCP_Quality": st.column_config.TextColumn("VCP", width="small"),
            "Pct_Above_50MA": st.column_config.NumberColumn("% > 50MA", format="%.1f%%", width="small"),
            "Price": st.column_config.NumberColumn("PRICE", format="$%.2f", width="small"),
            "Buy_Trigger": st.column_config.NumberColumn("BUY >", format="$%.2f", width="small"),
            "Stop": st.column_config.NumberColumn("STOP <", format="$%.2f", width="small"),
            "Risk_Pct": st.column_config.NumberColumn("RISK %", format="%.2f%%", width="small"),
            "Fundamentals": st.column_config.TextColumn("FUND", width="small"),
            "Market_Cap": st.column_config.TextColumn("MKT CAP", width="small"),
            "Sector": st.column_config.TextColumn("SECTOR", width="small"),
            "Industry": st.column_config.TextColumn("INDUSTRY"),
        }

        selection = st.dataframe(
            df, use_container_width=True, height=600,
            hide_index=True, column_config=column_config,
            on_select="rerun", selection_mode="single-row",
        )
        if selection and "selection" in selection and selection["selection"].get("rows"):
            idx = selection["selection"]["rows"][0]
            ticker = df.iloc[idx]["Ticker"]
            st.session_state.deep_dive_ticker = ticker
            st.session_state.view = "deepdive"
            st.rerun()
        return

    # =====================================================================
    # LAYER 1 — industry rotation overview
    # =====================================================================

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.5rem;font-weight:700;color:{cyan};margin-bottom:0.4rem'>"
        "🔄 INDUSTRY ROTATION</div>"
        f"<div style='color:{muted};font-size:0.9rem;line-height:1.5;margin-bottom:0.5rem'>"
        f"Industries ranked by mean RS. Δ1w / Δ4w columns show <b>where capital is "
        f"rotating to</b> — positive = climbing the leaderboard. Default sort is Rotation Score "
        f"(weighted toward 4-week rank change). Click any row to see the leader stocks within "
        f"that industry."
        f"</div>",
        unsafe_allow_html=True,
    )

    # Market regime banner (reuse the same signal_banner from longs)
    sig = scan.get("market_signal", "")
    if sig:
        subtitle = (
            f"S&P 500: {scan.get('sp_trend','?')} · Dist days: {scan.get('sp_dist','?')}  |  "
            f"Nasdaq: {scan.get('nq_trend','?')} · Dist days: {scan.get('nq_dist','?')}  |  "
            f"Russell 2000: {scan.get('rut_trend','?')} · Dist days: {scan.get('rut_dist','?')}"
        )
        st.markdown(signal_banner(sig, subtitle), unsafe_allow_html=True)

    # History availability note + source label
    short_hist = history.get("short")
    long_hist = history.get("long")
    short_note = "1w data: ✓" if short_hist else "1w data: not yet (need a scan from ~7 days ago)"
    long_note = "4w data: ✓" if long_hist else "4w data: not yet (need a scan from ~28 days ago)"
    source_label = {
        "quality":  "Source: Quality scans",
        "momentum": "Source: Momentum scans (fallback — run a Quality scan for better history)",
    }.get(rotation_source, "Source: —")
    st.markdown(
        f"<div style='color:{muted};font-size:0.78rem;margin:0.5rem 0 0.8rem 0;font-family:JetBrains Mono'>"
        f"{source_label} · {short_note} · {long_note}"
        f"</div>",
        unsafe_allow_html=True,
    )

    if rot_df.empty:
        st.info("Not enough industries in the latest scan (need ≥3 candidates per industry).")
        return

    # Filter row — sector dropdown + search + BUDDING LEADERS toggle.
    # BUDDING LEADERS (added 2026-06-30) is the actionable rotation filter:
    # industries with Δ4w ≥ +15 (confirmed positive rotation) AND current
    # rank between 15-50 (still in mid-pack, NOT yet the obvious leaders).
    # This is the "early-but-confirmed" zone — the rotation has begun and
    # capital is flowing, but the names haven't become consensus yet.
    if "rot_flt_budding" not in st.session_state:
        st.session_state["rot_flt_budding"] = False

    # Pre-compute the BUDDING count so the button label can show it
    budding_count = 0
    if "Delta_4w" in rot_df.columns and "Rank" in rot_df.columns:
        try:
            d4 = pd.to_numeric(rot_df["Delta_4w"], errors="coerce")
            rk = pd.to_numeric(rot_df["Rank"], errors="coerce")
            budding_count = int(((d4 >= 15) & rk.between(15, 50)).sum())
        except Exception:
            budding_count = 0

    fcol1, fcol2, fcol3 = st.columns([2, 4, 1.5])
    with fcol1:
        sectors = sorted(rot_df["Sector"].dropna().unique().tolist())
        sector_filter = st.selectbox(
            "Sector filter", options=["All sectors"] + sectors, index=0,
            help="Narrow the industry list to one GICS sector.",
        )
    with fcol2:
        search = st.text_input(
            "Search industry", placeholder="e.g. semi or oil",
            help="Filter rows by partial industry-name match (case-insensitive).",
        )
    with fcol3:
        # Vertical-align button with the input labels above
        st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
        bud_label = (
            f"🌱 BUDDING ({budding_count})"
            if st.session_state["rot_flt_budding"] else "🌱 BUDDING LEADERS"
        )
        if st.button(
            bud_label, type="primary", use_container_width=True, key="btn_rot_budding",
            help="Industries with confirmed 4-week rotation IN (Δ4w ≥ +15) that are "
                 "still in the mid-pack (current rank 15-50) — NOT yet the obvious "
                 "leaders. The actionable rotation zone: capital is flowing here but "
                 "the consensus hasn't caught up. Drill in to find clean Stage 2 "
                 "stocks within these industries.",
        ):
            st.session_state["rot_flt_budding"] = not st.session_state["rot_flt_budding"]
            st.rerun()

    filtered = rot_df.copy()
    if sector_filter and sector_filter != "All sectors":
        filtered = filtered[filtered["Sector"] == sector_filter]
    if search:
        s = search.upper()
        filtered = filtered[filtered["Industry"].astype(str).str.upper().str.contains(s, na=False)]
    if st.session_state["rot_flt_budding"]:
        if "Delta_4w" in filtered.columns and "Rank" in filtered.columns:
            d4 = pd.to_numeric(filtered["Delta_4w"], errors="coerce")
            rk = pd.to_numeric(filtered["Rank"], errors="coerce")
            filtered = filtered[(d4 >= 15) & rk.between(15, 50)]

    # Default sort: Rotation Score desc
    if "RotationScore" in filtered.columns:
        filtered = filtered.sort_values("RotationScore", ascending=False, na_position="last")

    # Column config — formatting the rotation table
    column_config = {
        "Industry": st.column_config.TextColumn("INDUSTRY", pinned=True),
        "Sector": st.column_config.TextColumn("SECTOR", width="small"),
        "Members": st.column_config.NumberColumn("MEMBERS", format="%d", width="small",
            help="Number of scan candidates in this industry"),
        "Rank": st.column_config.NumberColumn("CUR RANK", format="%d", width="small",
            help="Current rank by mean RS (1 = best)"),
        "Rank_1w": st.column_config.NumberColumn("RANK 1w", format="%d", width="small",
            help="Rank ~1 week ago (blank if no scan from that period)"),
        "Rank_4w": st.column_config.NumberColumn("RANK 4w", format="%d", width="small",
            help="Rank ~4 weeks ago (blank if no scan from that period)"),
        "Delta_1w": st.column_config.NumberColumn("Δ 1w", format="%+d", width="small",
            help="Positive = climbed the rankings since 1 week ago. The rotation signal."),
        "Delta_4w": st.column_config.NumberColumn("Δ 4w", format="%+d", width="small",
            help="Positive = climbed the rankings since 4 weeks ago. The structural rotation signal."),
        "MeanRS": st.column_config.NumberColumn("AVG RS", format="%.1f", width="small"),
        "PctAbove50MA": st.column_config.NumberColumn("% > 50MA", format="%.1f%%", width="small",
            help="Pct of industry members above their 50-day MA. Higher = broader strength."),
        "RotationScore": st.column_config.ProgressColumn("ROTATION", min_value=0, max_value=200,
            format="%.0f", width="small",
            help="Composite score: mean RS + 1.5×Δ4w + 0.75×Δ1w. Higher = stronger rotation IN."),
    }

    # Stash the filtered rotation df so the sidebar's Download Excel button
    # has something to export.
    st.session_state.rotation_filtered_df = filtered

    st.caption(f"Showing {len(filtered)} of {len(rot_df)} industries · sort ROTATION desc · click any row to drill in.")

    selection = st.dataframe(
        filtered, use_container_width=True, height=600,
        hide_index=True, column_config=column_config,
        on_select="rerun", selection_mode="single-row",
    )
    if selection and "selection" in selection and selection["selection"].get("rows"):
        idx = selection["selection"]["rows"][0]
        ind = filtered.iloc[idx]["Industry"]
        st.session_state.rotation_selected_industry = ind
        st.rerun()



# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

view = st.session_state.view
mode = st.session_state.get("mode", "top_stocks")

# Recover from stale modes that were deleted. shorts (2026-06-06) and early
# (2026-06-11) both removed. Anyone with one cached gets sent back to Top Stocks.
if mode in ("shorts", "early") or view in ("short_candidates", "early"):
    mode = "top_stocks"
    view = "top_stocks"
    st.session_state.mode = mode
    st.session_state.view = view

# Mode-aware view defaults
if mode == "top_stocks" and view not in ("top_stocks", "deepdive"):
    view = "top_stocks"
    st.session_state.view = view
elif mode == "journal" and view not in ("journal", "deepdive"):
    view = "journal"
    st.session_state.view = view
elif mode == "longs" and view in ("top_stocks", "journal"):
    view = "dashboard"
    st.session_state.view = view
elif mode == "research" and view not in ("research", "deepdive"):
    view = "research"
    st.session_state.view = view
elif mode == "twitter" and view not in ("twitter", "deepdive"):
    view = "twitter"
    st.session_state.view = view
elif mode == "rotation" and view not in ("rotation", "deepdive"):
    view = "rotation"
    st.session_state.view = view
elif mode == "quality" and view not in ("quality", "deepdive"):
    view = "quality"
    st.session_state.view = view
elif mode == "mean_reversion" and view not in ("mean_reversion", "deepdive"):
    view = "mean_reversion"
    st.session_state.view = view

if view == "deepdive" and st.session_state.deep_dive_ticker:
    render_deep_dive_page()
elif view == "top_stocks":
    _render_mode_switcher()
    render_top_stocks_page()
elif view == "journal":
    _render_mode_switcher()
    render_journal_page()
elif view == "research":
    render_research_page()
elif view == "twitter":
    render_twitter_page()
elif view == "rotation":
    render_rotation_page()
elif view == "quality":
    _render_mode_switcher()
    render_quality_page()
elif view == "mean_reversion":
    _render_mode_switcher()
    render_mean_reversion_page()
elif view == "diagnose":
    render_diagnose_page()
else:
    render_dashboard()

_render_sidebar()
