"""
Trade Journal UI v2 — position model with adds/partial exits.

Each row in the table is one POSITION (one ticker × one thesis). Inside that
position you can pyramid-add or scale-out via per-row "Add Shares" / "Sell
Shares" buttons. Status badges (OPEN / PARTIAL / CLOSED) reflect the
underlying transaction list.

Stop price is OPTIONAL. If you don't set one, the trade still logs and
P&L still computes — but R-multiple shows "—" because there's no risk
basis to anchor it to. Expectancy-by-setup only counts trades with a
defined stop.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional
import pandas as pd
import streamlit as st

from scanner.journal import (
    Trade,
    Transaction,
    SETUP_TAGS,
    load_trades,
    save_trades,
    add_trade,
    update_trade,
    delete_trade,
    expectancy_by_setup,
)
from .style import PALETTE, panel
from .excel_export import format_dataframe_to_excel


# ---------------------------------------------------------------------------
# Formatting helpers (display only — underlying data stays canonical)
# ---------------------------------------------------------------------------

def _fmt_date(date_str: str) -> str:
    """Convert a 'YYYY-MM-DD' or ISO date string into '11Jun2026' format.
    Returns the original string unchanged if parsing fails (e.g. already
    formatted, blank, or some legacy variant)."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    # Take just the date portion if there's a time/tz suffix (ISO timestamps)
    date_part = s.split("T", 1)[0].split(" ", 1)[0]
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").strftime("%d%b%Y")
    except Exception:
        pass
    # Fallback for fully-qualified ISO timestamps with offset
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%d%b%Y")
    except Exception:
        return s


def _fmt_money(value: float, decimals: int = 2, signed: bool = False) -> str:
    """Format a dollar amount with comma thousands separator.
    $1,200.00 not $1200.00. Returns '$0.00' for None/NaN."""
    try:
        v = float(value)
    except Exception:
        return f"${0:,.{decimals}f}"
    sign = "+" if signed else ""
    return f"${v:{sign},.{decimals}f}"


def _fmt_int(value) -> str:
    """Format an integer with comma thousands separator (e.g. 1,234)."""
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def _risk_pct(trade: Trade) -> Optional[float]:
    """INITIAL risk as % of starter entry: (starter - initial_stop) / starter × 100.

    Anchored to initial_stop_price (immutable), NOT the current trailing stop.
    Pairs with initial_risk_dollars — both reflect the R-multiple anchor, not
    current exposure. Falls back to stop_price for legacy trades with no
    initial_stop_price recorded.

    Returns None when no stop was set or stop >= starter price.
    """
    if not trade.buys:
        return None
    starter = trade.buys[0]
    anchor = trade.initial_stop_price if trade.initial_stop_price > 0 else trade.stop_price
    if anchor <= 0 or anchor >= starter.price:
        return None
    return round((starter.price - anchor) / starter.price * 100, 2)


def _live_risk_dollars(trade: Trade) -> Optional[float]:
    """LIVE dollar exposure if the position were stopped right now.

    Formula: (avg_entry - current_stop) × net_shares
      - Positive: dollars currently at risk
      - Zero: stop exactly at avg entry (breakeven)
      - Negative: stop above avg entry → locked-in profit ("PROTECTED")

    Updates as you trail stops, add shares, or scale out — unlike initial
    risk which is frozen at trade inception for R-multiple integrity.

    Returns None when there's no current stop or no held shares.
    """
    if trade.stop_price <= 0 or trade.net_shares == 0 or trade.avg_entry_price <= 0:
        return None
    return round((trade.avg_entry_price - trade.stop_price) * trade.net_shares, 2)


def _live_risk_pct(trade: Trade) -> Optional[float]:
    """LIVE stop distance from current avg entry as a percentage:
    (avg_entry - current_stop) / avg_entry × 100. Negative when stop is
    above avg entry (locked-in profit). Updates with every stop trail."""
    if trade.stop_price <= 0 or trade.avg_entry_price <= 0:
        return None
    return round((trade.avg_entry_price - trade.stop_price) / trade.avg_entry_price * 100, 2)


def _refresh_journal_prices(trades: list[Trade]) -> tuple[int, int]:
    """Fetch fresh price history for every ticker with held shares, save each
    to the local price cache. Returns (successes, failures).

    Why this exists: Last $ reads from data/cache/prices/<ticker>.pkl, which is
    only refreshed when a full scan runs. A user who just wants up-to-date P&L
    on their journal positions shouldn't have to run a 25-minute Momentum scan
    to refresh ~10 tickers. This targeted fetch covers only the held tickers
    and typically completes in 10-30 seconds.
    """
    # Held tickers only — closed positions don't show Last $ anyway
    tickers = sorted({t.ticker for t in trades if t.net_shares > 0})
    if not tickers:
        return 0, 0

    try:
        import yfinance as yf
        from datetime import datetime as _dt, timedelta as _td
        from scanner.data_cache import save_price_history
    except ImportError as e:
        st.error(f"Can't refresh — missing dependency: {e}")
        return 0, 0

    bar = st.progress(0.0, text=f"Refreshing prices for {len(tickers)} tickers…")
    succ, fail = 0, 0

    # 540-day window matches what other scanners pull, so the cache stays
    # compatible with MA/RSI calculations that need 200+ bars.
    start_date = (_dt.now() - _td(days=540)).strftime("%Y-%m-%d")

    for i, ticker in enumerate(tickers):
        try:
            hist = yf.Ticker(ticker).history(start=start_date, interval="1d")
            if hist is None or hist.empty:
                fail += 1
            else:
                # Flatten multi-index columns if yfinance returned them
                if hasattr(hist.columns, "get_level_values"):
                    try:
                        hist.columns = hist.columns.get_level_values(0)
                    except Exception:
                        pass
                save_price_history(ticker, hist)
                succ += 1
        except Exception:
            fail += 1
        bar.progress(
            (i + 1) / len(tickers),
            text=f"Refreshed {i + 1}/{len(tickers)} ({ticker})",
        )

    bar.empty()
    return succ, fail


def _last_close_for(ticker: str) -> Optional[tuple[float, str]]:
    """Read the most recent cached (close, date) from data/cache/prices/<ticker>.pkl.

    Returns a (price, date_str) tuple where date_str is formatted DDmmmYYYY
    (e.g. "13Jun2026"). Returns None when:
      - The ticker isn't cached yet (no scan ever fetched it)
      - The cached file is corrupt or empty
      - The Close column is NaN

    The date is exposed as a separate column on the journal table so the user
    can see whether the cached close is fresh (yesterday's date) or stale
    (last Friday's date, last week's date) without having to hover or guess.
    """
    try:
        from scanner.data_cache import load_price_history
        df = load_price_history(ticker)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        # Defensive multi-index column flattening (yfinance batched downloads)
        close = df["Close"]
        if hasattr(close, "columns") or close.ndim > 1:
            close = close.iloc[:, 0]
        val = float(close.iloc[-1])
        if pd.isna(val):
            return None
        # Extract the date of the last bar (DatetimeIndex)
        last_idx = close.index[-1]
        try:
            date_str = last_idx.strftime("%d%b%Y")
        except Exception:
            # Fallback for non-datetime indexes — just slice the string
            date_str = str(last_idx)[:10]
        return round(val, 2), date_str
    except Exception:
        return None


def _protected_green(val) -> str:
    """Pandas Styler callback for the LIVE risk columns: color non-positive
    values green to flag the position as PROTECTED (stop above avg entry —
    can't take a loss from here, only a smaller-than-expected gain).
    Positive (still-at-risk) values stay at the default color."""
    if val is None:
        return ""
    try:
        v = float(val)
        return "color: #00FF88; font-weight: 600;" if v <= 0 else ""
    except (ValueError, TypeError):
        return ""


def _negative_red(val) -> str:
    """Pandas Styler callback: color negatives red, leave non-negatives default.

    Handles both numeric cells (Realized $, P&L $ — stored as floats) and
    string cells (R column — stored as '+0.50' / '—' / '-1.20'). Returns
    a CSS string for Styler to apply at render time.

    Positive values are intentionally left at the table's default color
    rather than forced to black (black-on-dark-background would be unreadable
    in this app's Bloomberg dark theme — request was for visual differentiation
    of negatives, which red provides cleanly)."""
    if val is None:
        return ""
    try:
        if isinstance(val, str):
            s = val.replace("$", "").replace(",", "").replace("+", "").strip()
            if not s or s in ("—", "-"):
                return ""
            v = float(s)
        else:
            v = float(val)
        return "color: #FF4D6D; font-weight: 600;" if v < 0 else ""
    except (ValueError, TypeError):
        return ""


def _render_held_totals(trades: list[Trade]) -> None:
    """One-line summary strip showing aggregate totals across held positions.

    Lives below the journal table. Sums the same per-row quantities the user
    sees in the Cost $ / P&L $ / G/L % columns, so the totals row reconciles
    line-by-line with the table above. Skipped silently if there are no held
    positions (all closed).
    """
    held = [t for t in trades if t.net_shares > 0]
    if not held:
        return

    cyan  = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    up    = PALETTE["up_green"]
    down  = PALETTE["down_red"]

    total_cost_basis        = 0.0
    total_current_value     = 0.0
    total_lifetime_invested = 0.0
    total_pnl               = 0.0
    total_live_risk         = 0.0
    positions_protected     = 0
    missing_price_count     = 0

    for t in held:
        total_cost_basis        += t.cost_basis
        total_lifetime_invested += t.avg_entry_price * t.total_shares_bought
        last_result = _last_close_for(t.ticker)
        last_price  = last_result[0] if last_result else None
        if last_price is not None:
            total_current_value += last_price * t.net_shares
            pnl = _open_pnl(t, last_price)
            if pnl is not None:
                total_pnl += pnl
        else:
            missing_price_count += 1
        # Sum live risk across all held positions. Negative entries (protected
        # positions where stop is above avg entry) reduce the at-risk total —
        # that's intentional. The number answers "if EVERY stop hit today, how
        # much would I lose net?" — which is the real account-level exposure.
        lr = _live_risk_dollars(t)
        if lr is not None:
            total_live_risk += lr
            if lr <= 0:
                positions_protected += 1

    # G/L % denominator matches the per-row formula (lifetime invested capital).
    gl_pct = (total_pnl / total_lifetime_invested * 100) if total_lifetime_invested > 0 else 0.0
    pnl_color  = up if total_pnl > 0 else (down if total_pnl < 0 else muted)
    risk_color = up if total_live_risk <= 0 else cyan

    missing_note = ""
    if missing_price_count:
        missing_note = (
            f" <span style='color:{muted};font-size:0.78rem'>"
            f"(price missing on {missing_price_count} ticker{'s' if missing_price_count != 1 else ''} "
            f"— click Refresh Prices in the sidebar)</span>"
        )
    protected_tag = ""
    if positions_protected:
        protected_tag = (
            f" <span style='color:{up};font-size:0.78rem'>"
            f"({positions_protected} protected)</span>"
        )

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.92rem;"
        f"background:#0F1620;border:1px solid #1F2937;border-radius:6px;"
        f"padding:0.75rem 1rem;margin-top:0.6rem;line-height:1.7'>"
        f"<b style='color:{cyan};letter-spacing:0.04em'>HELD POSITIONS ({len(held)})</b>"
        f" &nbsp;&middot;&nbsp; Cost: <b>{_fmt_money(total_cost_basis, 0)}</b>"
        f" &nbsp;&middot;&nbsp; Value: <b>{_fmt_money(total_current_value, 0)}</b>"
        f" &nbsp;&middot;&nbsp; P&amp;L: "
        f"<b style='color:{pnl_color}'>{_fmt_money(total_pnl, 0, signed=True)}</b>"
        f" &nbsp;<b style='color:{pnl_color}'>({gl_pct:+.1f}%)</b>"
        f" &nbsp;&middot;&nbsp; Live Risk: "
        f"<b style='color:{risk_color}'>{_fmt_money(total_live_risk, 0, signed=True)}</b>"
        f"{protected_tag}"
        f"{missing_note}"
        f"</div>",
        unsafe_allow_html=True,
    )


def _gain_loss_pct(trade: Trade, pnl: Optional[float]) -> Optional[float]:
    """Total P&L as a % of capital invested across the life of the position.

    Denominator = avg_entry_price × total_shares_bought (cost basis of ALL
    shares ever bought, held or sold). One formula that's meaningful across
    OPEN (return on currently-held cost), PARTIAL (return on full lifetime
    invested), and CLOSED (realized return on capital).

    Returns None when we can't compute (no buys, missing P&L, or zero cost).
    """
    if pnl is None:
        return None
    if trade.total_shares_bought == 0 or trade.avg_entry_price <= 0:
        return None
    total_invested = trade.avg_entry_price * trade.total_shares_bought
    if total_invested <= 0:
        return None
    return round(pnl / total_invested * 100, 2)


def _open_pnl(trade: Trade, last_close: Optional[float]) -> Optional[float]:
    """Total P&L for the position right now: realized + (last - avg_entry) × held.

    For CLOSED positions there are no held shares, so this equals realized_pnl.
    For OPEN positions realized_pnl is 0, so this equals the unrealized.
    For PARTIAL positions both pieces matter and we sum them.
    Returns None if we can't get a last close AND there are still held shares
    (otherwise we'd silently report 'just the realized portion' as if it were
    the full picture)."""
    if trade.net_shares == 0:
        return trade.realized_pnl  # closed position
    if last_close is None:
        return None  # can't compute unrealized without a price
    unrealized = (last_close - trade.avg_entry_price) * trade.net_shares
    return round(trade.realized_pnl + unrealized, 2)


# ---------------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------------

def _render_summary_tiles(trades: list[Trade]):
    """Five hover-card tiles showing journal-wide stats."""
    closed = [t for t in trades if t.status == "closed"]
    partial = [t for t in trades if t.status == "partial"]
    open_full = [t for t in trades if t.status == "open"]

    # Win rate / expectancy: only over trades with computable R
    rated = [t for t in closed if t.realized_r is not None]
    wins = [t for t in rated if t.realized_r > 0]
    losses = [t for t in rated if t.realized_r <= 0]

    win_rate = (len(wins) / len(rated) * 100) if rated else 0.0
    avg_win_r = (sum(t.realized_r for t in wins) / len(wins)) if wins else 0.0
    avg_loss_r = (sum(t.realized_r for t in losses) / len(losses)) if losses else 0.0
    expectancy = ((win_rate / 100) * avg_win_r
                  + (1 - win_rate / 100) * avg_loss_r) if rated else 0.0
    # Realized P&L sums over ALL closed + partial trades (they have real P&L)
    total_pnl = sum(t.realized_pnl for t in (closed + partial))

    css = """
<style>
.jr-grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 0.6rem;
    margin: 0.4rem 0 1rem 0;
}
.jr-card {
    background: #0F1620;
    border: 1px solid #1F2937;
    border-radius: 6px;
    padding: 0.7rem 0.6rem 0.75rem 0.6rem;
    text-align: center;
}
.jr-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #FFFFFF;
    font-weight: 600;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
}
.jr-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    color: #FFFFFF;
    line-height: 1.1;
}
.jr-value.up   { color: #00FF88; }
.jr-value.down { color: #FF4D6D; }
.jr-sub {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: #8B95A5;
    margin-top: 0.25rem;
}
</style>
"""
    pnl_class = "up" if total_pnl > 0 else ("down" if total_pnl < 0 else "")
    exp_class = "up" if expectancy > 0 else ("down" if expectancy < 0 else "")

    # Open count combines fully-open + partial (both still have shares held)
    open_count = len(open_full) + len(partial)
    partial_note = f"({len(partial)} partial)" if partial else ""

    html = f"""
{css}
<div class='jr-grid'>
  <div class='jr-card'>
    <div class='jr-label'>OPEN POSITIONS</div>
    <div class='jr-value'>{open_count}</div>
    <div class='jr-sub'>{partial_note}</div>
  </div>
  <div class='jr-card'>
    <div class='jr-label'>CLOSED</div>
    <div class='jr-value'>{len(closed)}</div>
  </div>
  <div class='jr-card'>
    <div class='jr-label'>WIN RATE</div>
    <div class='jr-value'>{win_rate:.0f}%</div>
    <div class='jr-sub'>({len(rated)} rated)</div>
  </div>
  <div class='jr-card'>
    <div class='jr-label'>EXPECTANCY (R)</div>
    <div class='jr-value {exp_class}'>{expectancy:+.2f}</div>
  </div>
  <div class='jr-card'>
    <div class='jr-label'>REALIZED P&L</div>
    <div class='jr-value {pnl_class}'>${total_pnl:,.0f}</div>
  </div>
</div>
"""
    st.markdown(html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Add-trade form
# ---------------------------------------------------------------------------

def _render_add_form():
    """Form to log a new OPEN position (one starter BUY transaction)."""
    with st.expander("➕ Log a new trade", expanded=False):
        with st.form("journal_add_trade", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                ticker = st.text_input("Ticker *", placeholder="e.g. NVDA")
                setup = st.selectbox("Setup tag *", SETUP_TAGS)
            with c2:
                entry_date = st.date_input("Entry date *", value=date.today())
                entry_price = st.number_input(
                    "Entry price ($) *", min_value=0.0, value=0.0,
                    step=0.01, format="%.4f",
                )
            with c3:
                stop_price = st.number_input(
                    "Stop price ($) — optional",
                    min_value=0.0, value=0.0, step=0.01, format="%.4f",
                    help="Leave at 0 if you don't have a predefined stop. "
                         "R-multiple won't compute for this trade, but P&L still will.",
                )
                shares = st.number_input(
                    "Shares *", min_value=0, value=0, step=1,
                )

            notes = st.text_area("Notes (optional)",
                                  placeholder="What's the thesis? What confirmed it?",
                                  height=70)

            submitted = st.form_submit_button(
                "Save Trade", type="primary", use_container_width=True,
            )
            if submitted:
                # Loud, prominent validation errors — easier to notice than
                # the v1 silent rejections.
                errs = []
                if not ticker.strip():
                    errs.append("Ticker is required.")
                if entry_price <= 0:
                    errs.append("Entry price must be > 0.")
                if shares <= 0:
                    errs.append("Shares must be > 0.")
                if stop_price > 0 and stop_price >= entry_price:
                    errs.append(
                        "Stop price must be BELOW entry price (this is a long trade). "
                        "Leave stop at 0 if you don't want one."
                    )
                if errs:
                    for e in errs:
                        st.error(e)
                else:
                    t = Trade.new(
                        ticker=ticker,
                        setup_tag=setup,
                        entry_date=str(entry_date),
                        entry_price=entry_price,
                        stop_price=stop_price,  # 0 is fine — means "no stop"
                        shares=int(shares),
                        notes=notes,
                    )
                    add_trade(t)
                    if t.initial_risk_dollars > 0:
                        rp = _risk_pct(t)
                        rp_str = f" ({rp:.1f}%)" if rp is not None else ""
                        stop_msg = f" — risk {_fmt_money(t.initial_risk_dollars, 0)}{rp_str}"
                    else:
                        stop_msg = " (no stop — R-multiple won't compute)"
                    st.success(f"Logged {t.ticker} ({t.setup_tag}){stop_msg}")
                    st.rerun()


# ---------------------------------------------------------------------------
# Add Shares / Sell Shares forms (per-row actions)
# ---------------------------------------------------------------------------

def _render_add_shares_form(trade: Trade):
    """Form to record a pyramid-add BUY on an existing position."""
    with st.form(f"add_shares_{trade.id}", clear_on_submit=True):
        st.markdown(f"**Add shares to {trade.ticker}** (current holding: "
                    f"{_fmt_int(trade.net_shares)} shares @ avg {_fmt_money(trade.avg_entry_price, 2)})")
        c1, c2, c3 = st.columns(3)
        with c1:
            add_date = st.date_input("Date", value=date.today(), key=f"add_dt_{trade.id}")
        with c2:
            add_price = st.number_input(
                "Price per share ($)", min_value=0.0,
                value=float(trade.avg_entry_price), step=0.01, format="%.4f",
                key=f"add_px_{trade.id}",
            )
        with c3:
            add_shares_n = st.number_input(
                "Shares to add", min_value=0, value=0, step=1,
                key=f"add_sh_{trade.id}",
            )
        add_note = st.text_area(
            "Add to notes (optional)",
            placeholder="e.g. confirmation on heavy volume — bumping to full size",
            height=70, key=f"add_note_{trade.id}",
            help="Appended to the position's Notes section as a timestamped line.",
        )
        sub1, sub2 = st.columns(2)
        with sub1:
            do_add = st.form_submit_button("Add Shares", type="primary",
                                            use_container_width=True)
        with sub2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

        if do_add:
            if add_price <= 0 or add_shares_n <= 0:
                st.error("Price and shares must both be > 0")
            else:
                def _mutate(t):
                    t.add_buy(str(add_date), add_price, int(add_shares_n))
                    if add_note and add_note.strip():
                        action = f"ADD {int(add_shares_n)} @ ${add_price:,.2f}"
                        t.append_note(action, add_note, str(add_date))
                update_trade(trade.id, _mutate)
                st.session_state.pop(f"adding_{trade.id}", None)
                st.success(f"Added {_fmt_int(int(add_shares_n))} shares of {trade.ticker} @ {_fmt_money(add_price, 2)}")
                st.rerun()
        elif cancel:
            st.session_state.pop(f"adding_{trade.id}", None)
            st.rerun()


def _snapshot_compute(trade: Trade) -> dict:
    """Compute all Decision Snapshot metrics for one position.

    Returns a dict with four sections (trade_state, stock_state, recent, context),
    each a list of {label, value, color, note} dicts, plus a tally count.

    'color' is one of: 'green' / 'yellow' / 'red' / 'info' / 'neutral'.
    'info' and 'neutral' don't count toward the tally — green/yellow/red do.

    Recomputes every call (~50ms per ticker) so the snapshot always reflects
    the freshest data in the cache. No caching — keeps things simple and
    ensures Refresh Prices changes show up immediately.
    """
    out = {
        "available": False, "reason": "",
        "trade_state": [], "stock_state": [], "recent": [], "context": [],
        "tally": {"green": 0, "yellow": 0, "red": 0},
    }

    try:
        from scanner.data_cache import load_price_history
    except Exception:
        out["reason"] = "Couldn't load price-cache module."
        return out

    price_df = load_price_history(trade.ticker)
    if price_df is None or price_df.empty or "Close" not in price_df.columns:
        out["reason"] = (
            f"**{trade.ticker} isn't in the local price cache yet.** "
            f"This ticker hasn't been touched by any scan. Either run a Momentum "
            f"or Quality scan (which will pull every Russell 3000 stock), or click "
            f"**Refresh Prices** in the sidebar (which just pulls your held tickers). "
            f"Then re-open the Snapshot."
        )
        return out

    def _flat(col_name):
        s = price_df[col_name]
        if hasattr(s, "columns") or s.ndim > 1:
            s = s.iloc[:, 0]
        return s
    close = _flat("Close")
    if len(close) < 50:
        out["reason"] = (
            f"Not enough cached price history for {trade.ticker} "
            f"(need 50+ bars, have {len(close)}). Run a fresh scan first."
        )
        return out
    high   = _flat("High")
    low    = _flat("Low")
    volume = _flat("Volume")

    out["available"] = True
    last_close = float(close.iloc[-1])

    # ============================================================
    # SECTION 1 — TRADE STATE
    # ============================================================
    r_val = trade.realized_r
    r_str = f"{r_val:+.2f}R" if r_val is not None else "—"
    out["trade_state"].append({
        "label": "Realized R-multiple", "value": r_str,
        "color": "info", "note": "Per-share R on shares already sold",
    })

    # Days since entry
    try:
        from datetime import datetime as _dt, date as _dt_date
        entry_d = _dt.strptime(trade.entry_date, "%Y-%m-%d").date()
        days_held = (_dt_date.today() - entry_d).days
    except Exception:
        days_held = 0
    if days_held < 15:
        col, note = "neutral", "Early in the trade"
    elif days_held <= 40:
        col, note = "yellow", "Mid-cycle"
    else:
        col, note = "neutral", "Mature trend — let it tell you"
    out["trade_state"].append({
        "label": "Days since entry", "value": f"{days_held} days",
        "color": col, "note": note,
    })

    # Stop relative to entry
    starter_price = trade.buys[0].price if trade.buys else 0.0
    if trade.stop_price <= 0:
        col, val, note = "yellow", "no stop set", "Define a protective stop"
    elif starter_price > 0 and trade.stop_price < starter_price - 0.005:
        diff_pct = (starter_price - trade.stop_price) / starter_price * 100
        col, val, note = "yellow", f"-{diff_pct:.1f}% below entry", "Original protective stop"
    elif starter_price > 0 and abs(trade.stop_price - starter_price) / starter_price < 0.005:
        col, val, note = "green", "at entry", "Breakeven — cannot become a loser"
    else:
        diff_pct = (trade.stop_price - starter_price) / starter_price * 100 if starter_price > 0 else 0
        col, val, note = "green", f"+{diff_pct:.1f}% above entry", "Profit locked in"
    out["trade_state"].append({
        "label": "Stop relative to entry", "value": val,
        "color": col, "note": note,
    })

    # % of starter sold
    if trade.buys and trade.buys[0].shares > 0:
        sold_pct = trade.total_shares_sold / trade.buys[0].shares * 100
        if sold_pct == 0:
            col, note = "neutral", "Nothing scaled yet"
        elif sold_pct < 50:
            col, note = "green", "Partial profit taken"
        elif sold_pct < 100:
            col, note = "green", "Mostly scaled"
        else:
            col, note = "neutral", "Fully scaled / closed"
        out["trade_state"].append({
            "label": "% of starter sold", "value": f"{sold_pct:.0f}%",
            "color": col, "note": note,
        })

    # ============================================================
    # SECTION 2 — STOCK STATE
    # ============================================================
    sma_50 = float(close.tail(50).mean())
    ema_21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])

    pct_above_50 = ((last_close / sma_50) - 1) * 100 if sma_50 > 0 else 0.0
    if pct_above_50 < 5:
        col, note = "green", "Near support — room to add"
    elif pct_above_50 < 15:
        col, note = "yellow", "Healthy distance"
    elif pct_above_50 < 20:
        col, note = "yellow", "Getting extended"
    else:
        col, note = "red", "Extended — consider scaling"
    out["stock_state"].append({
        "label": "% above 50MA", "value": f"{pct_above_50:+.1f}%",
        "color": col, "note": note,
    })

    pct_above_21 = ((last_close / ema_21) - 1) * 100 if ema_21 > 0 else 0.0
    if pct_above_21 < 5:
        col, note = "green", "At trend support"
    elif pct_above_21 < 10:
        col, note = "yellow", "Moderate distance"
    else:
        col, note = "red", "Short-term extended"
    out["stock_state"].append({
        "label": "% above 21EMA", "value": f"{pct_above_21:+.1f}%",
        "color": col, "note": note,
    })

    # RSI(14) inline
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).ewm(span=14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(span=14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi_val = float((100 - 100 / (1 + rs)).iloc[-1])
    if rsi_val < 60:
        col, note = "green", "Room to run"
    elif rsi_val < 70:
        col, note = "yellow", "Moderate momentum"
    elif rsi_val < 80:
        col, note = "yellow", "Approaching overbought"
    else:
        col, note = "red", "Overbought"
    out["stock_state"].append({
        "label": "RSI(14)", "value": f"{rsi_val:.1f}",
        "color": col, "note": note,
    })

    # Distance from avg entry (proxy for breakout pivot)
    if trade.avg_entry_price > 0:
        dist_pct = ((last_close / trade.avg_entry_price) - 1) * 100
        if dist_pct < 10:
            col, note = "yellow", "Mid-cycle"
        elif dist_pct < 25:
            col, note = "yellow", "Climbing"
        else:
            col, note = "red", "Extended from pivot"
        out["stock_state"].append({
            "label": "Distance from avg entry", "value": f"{dist_pct:+.1f}%",
            "color": col, "note": note,
        })

    # ============================================================
    # SECTION 3 — RECENT ACTION (last 5 days)
    # ============================================================
    daily_range = (high - low).abs()
    if len(daily_range) >= 25:
        last5_rng = float(daily_range.tail(5).mean())
        prior20_rng = float(daily_range.iloc[-25:-5].mean())
        if prior20_rng > 0:
            rng_ratio = last5_rng / prior20_rng
            if rng_ratio < 1.0:
                col, note = "green", "Contracting — constructive"
            elif rng_ratio < 1.3:
                col, note = "yellow", "Slightly widening"
            else:
                col, note = "red", "Widening — climactic risk"
            out["recent"].append({
                "label": "5d range vs prior 20d", "value": f"{rng_ratio:.2f}x",
                "color": col, "note": note,
            })

    # Volume last 5d vs 50d avg
    if len(volume) >= 50:
        last5_vol = float(volume.tail(5).mean())
        last50_vol = float(volume.tail(50).mean())
        if last50_vol > 0:
            vol_ratio = last5_vol / last50_vol
            if vol_ratio < 1.0:
                col, note = "green", "Drying up — constructive"
            elif vol_ratio < 1.5:
                col, note = "yellow", "Elevated"
            else:
                col, note = "red", "Heavy — check for distribution"
            out["recent"].append({
                "label": "5d vol vs 50d avg", "value": f"{vol_ratio:.2f}x",
                "color": col, "note": note,
            })

    # Closes in upper-half of daily range
    last_5_high = high.tail(5).values
    last_5_low = low.tail(5).values
    last_5_close = close.tail(5).values
    upper_half = 0
    for h_, l_, c_ in zip(last_5_high, last_5_low, last_5_close):
        if h_ > l_ and float(c_) > (float(h_) + float(l_)) / 2:
            upper_half += 1
    if upper_half >= 4:
        col, note = "green", "Buyers in control"
    elif upper_half >= 2:
        col, note = "yellow", "Mixed control"
    else:
        col, note = "red", "Sellers in control"
    out["recent"].append({
        "label": "Closes in upper half", "value": f"{upper_half} of 5",
        "color": col, "note": note,
    })

    # Up-day count (informational)
    up_days = 0
    last_6_close = close.tail(6).values
    for i in range(1, len(last_6_close)):
        if float(last_6_close[i]) > float(last_6_close[i - 1]):
            up_days += 1
    out["recent"].append({
        "label": "Up days last 5", "value": f"{up_days} of 5",
        "color": "info", "note": "Informational",
    })

    # ============================================================
    # SECTION 4 — CONTEXT
    # ============================================================
    industry_rank = None
    market_signal = ""
    in_long = False
    in_quality_elite = False
    try:
        from scanner.cache import load_latest_scan
        long_scan = load_latest_scan() or {}
        market_signal = (long_scan.get("breadth_status", "")
                          or long_scan.get("market_signal", ""))
        for c in long_scan.get("candidates", []):
            if c.get("Ticker") == trade.ticker:
                in_long = True
                industry_rank = c.get("Industry_Rank")
                break
    except Exception:
        pass
    try:
        from scanner.quality import load_latest_quality_scan
        q_scan = load_latest_quality_scan() or {}
        for c in q_scan.get("candidates", []):
            if c.get("Ticker") == trade.ticker:
                if (c.get("Composite_Rating") or 0) >= 80:
                    in_quality_elite = True
                break
    except Exception:
        pass

    if industry_rank is not None:
        if industry_rank <= 15:
            col, note = "green", "Leading group"
        elif industry_rank <= 50:
            col, note = "yellow", "Mid-pack"
        else:
            col, note = "red", "Lagging group"
        out["context"].append({
            "label": "Industry rank", "value": f"#{int(industry_rank)}",
            "color": col, "note": note,
        })
    else:
        out["context"].append({
            "label": "Industry rank", "value": "—",
            "color": "info", "note": f"{trade.ticker} not in latest Momentum scan",
        })

    sig_upper = market_signal.upper()
    if "GREEN" in sig_upper:
        col, val, note = "green", market_signal, "Supportive backdrop"
    elif "YELLOW" in sig_upper:
        col, val, note = "yellow", market_signal, "Mixed market"
    elif "RED" in sig_upper:
        col, val, note = "red", market_signal, "Defensive — be cautious adding"
    else:
        col, val, note = "info", "—", "No market signal available"
    out["context"].append({
        "label": "Market signal", "value": val,
        "color": col, "note": note,
    })

    if in_long and in_quality_elite:
        col, val, note = "green", "YES", "On the elite intersection list"
    elif in_long or in_quality_elite:
        col, val, note = "yellow", "PARTIAL", "On one side but not both"
    else:
        col, val, note = "yellow", "NO", "Outside the elite intersection"
    out["context"].append({
        "label": "In Top Stocks intersection", "value": val,
        "color": col, "note": note,
    })

    # ============================================================
    # TALLY
    # ============================================================
    for section in (out["trade_state"], out["stock_state"], out["recent"], out["context"]):
        for item in section:
            c = item["color"]
            if c == "green":
                out["tally"]["green"] += 1
            elif c == "yellow":
                out["tally"]["yellow"] += 1
            elif c == "red":
                out["tally"]["red"] += 1

    return out


def _render_decision_snapshot(trade: Trade) -> None:
    """Render the inline Decision Snapshot panel for a position."""
    cyan   = PALETTE["accent_cyan"]
    muted  = PALETTE["text_muted"]
    up     = PALETTE["up_green"]
    down   = PALETTE["down_red"]
    yellow = PALETTE["signal_yellow"]

    st.markdown(f"##### 📊 Decision Snapshot — {trade.ticker}")
    data = _snapshot_compute(trade)

    if not data["available"]:
        st.info(data["reason"])
        if st.button("Close", key=f"snap_close_{trade.id}", use_container_width=True):
            st.session_state.pop(f"showing_snapshot_{trade.id}", None)
            st.rerun()
        return

    color_map = {"green": "🟢", "yellow": "🟡", "red": "🔴",
                  "info": "·", "neutral": "·"}

    def _section_html(title: str, items: list) -> str:
        rows = []
        for item in items:
            dot = color_map.get(item["color"], "·")
            rows.append(
                f"<tr>"
                f"<td style='padding:0.22rem 0.6rem 0.22rem 0;color:#E5E7EB;"
                f"font-size:0.85rem'>{item['label']}</td>"
                f"<td style='padding:0.22rem 0.6rem;font-family:JetBrains Mono;"
                f"font-size:0.85rem;color:#FFFFFF;text-align:right;white-space:nowrap'>{item['value']}</td>"
                f"<td style='padding:0.22rem 0.4rem;font-size:0.95rem;"
                f"text-align:center;width:30px'>{dot}</td>"
                f"<td style='padding:0.22rem 0.4rem;color:{muted};"
                f"font-size:0.78rem;font-style:italic'>{item['note']}</td>"
                f"</tr>"
            )
        return (
            f"<div style='font-family:JetBrains Mono;font-size:0.75rem;"
            f"font-weight:700;color:{cyan};letter-spacing:0.06em;"
            f"margin-top:0.7rem;margin-bottom:0.25rem'>{title}</div>"
            f"<table style='width:100%;border-collapse:collapse'>"
            + "".join(rows) +
            f"</table>"
        )

    panel_html = (
        f"<div style='background:#0F1620;border:1px solid #1F2937;border-radius:6px;"
        f"padding:0.8rem 1rem;margin-top:0.4rem'>"
        + _section_html("TRADE STATE", data["trade_state"])
        + _section_html("STOCK STATE", data["stock_state"])
        + _section_html("RECENT ACTION (last 5 days)", data["recent"])
        + _section_html("CONTEXT", data["context"])
        + "</div>"
    )
    st.markdown(panel_html, unsafe_allow_html=True)

    t = data["tally"]
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.0rem;text-align:center;"
        f"margin-top:0.7rem;letter-spacing:0.05em'>"
        f"<b style='color:{cyan}'>TALLY:</b> &nbsp;&nbsp;"
        f"<span style='color:{up};font-weight:700'>{t['green']} 🟢</span> &nbsp;·&nbsp;"
        f"<span style='color:{yellow};font-weight:700'>{t['yellow']} 🟡</span> &nbsp;·&nbsp;"
        f"<span style='color:{down};font-weight:700'>{t['red']} 🔴</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown("")
    if st.button("Close Snapshot", key=f"snap_close_{trade.id}",
                  use_container_width=True):
        st.session_state.pop(f"showing_snapshot_{trade.id}", None)
        st.rerun()


def _render_move_stop_form(trade: Trade):
    """Form to update a position's stop. Records the change in stop_history."""
    with st.form(f"move_stop_{trade.id}", clear_on_submit=True):
        # Header showing current state
        cur_stop_str = (_fmt_money(trade.stop_price, 2)
                        if trade.stop_price > 0 else "no stop set")
        init_stop_str = (_fmt_money(trade.initial_stop_price, 2)
                         if trade.initial_stop_price > 0 else "—")
        st.markdown(f"**Move stop on {trade.ticker}**")
        st.caption(
            f"Current stop: **{cur_stop_str}** · "
            f"Initial stop (R-multiple anchor, immutable): **{init_stop_str}** · "
            f"Avg entry: **{_fmt_money(trade.avg_entry_price, 2)}**"
        )

        c1, c2 = st.columns(2)
        with c1:
            update_date = st.date_input("Date", value=date.today(),
                                         key=f"stop_dt_{trade.id}")
        with c2:
            # Default the input to the current stop so a single-character edit
            # is fast (don't make user retype the whole number)
            default_val = float(trade.stop_price) if trade.stop_price > 0 else float(trade.avg_entry_price)
            new_stop = st.number_input(
                "New stop ($)", min_value=0.0,
                value=default_val, step=0.01, format="%.4f",
                key=f"stop_px_{trade.id}",
            )

        note = st.text_area(
            "Add to notes (optional)",
            placeholder="e.g. trailed under last week's swing low at $42.18",
            height=70, key=f"stop_note_{trade.id}",
            help="Appears in the Stop history table AND appended to the "
                 "position's Notes section as a timestamped line.",
        )

        # Pre-submit warnings — non-blocking but loud
        if 0 < new_stop < trade.stop_price:
            st.warning(
                f"⚠ This LOWERS your stop from {_fmt_money(trade.stop_price, 2)} "
                f"to {_fmt_money(new_stop, 2)}. Smart trading raises stops to "
                f"protect profits, never lowers them. Override only if you have "
                f"a very specific reason."
            )
        elif new_stop > 0 and trade.avg_entry_price > 0 and new_stop > trade.avg_entry_price * 1.01:
            # Stop above entry = locking in profit (good)
            locked_pct = (new_stop / trade.avg_entry_price - 1) * 100
            st.info(
                f"✓ This sets the stop above your avg entry "
                f"({_fmt_money(trade.avg_entry_price, 2)}) — you're locking in "
                f"≈{locked_pct:.1f}% profit on the held shares."
            )

        sub1, sub2 = st.columns(2)
        with sub1:
            do_update = st.form_submit_button(
                "Move Stop", type="primary", use_container_width=True,
            )
        with sub2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

        if do_update:
            ok_msg = {"ok": True, "msg": ""}
            def _mutate(t):
                ok, msg = t.update_stop(str(update_date), float(new_stop), note)
                ok_msg["ok"] = ok
                ok_msg["msg"] = msg
                # Also append to the position-level Notes so the running
                # narrative captures the stop move alongside add/sell events.
                if ok and note and note.strip():
                    action = f"STOP → ${float(new_stop):,.2f}"
                    t.append_note(action, note, str(update_date))
            update_trade(trade.id, _mutate)
            if not ok_msg["ok"]:
                st.error(ok_msg["msg"])
            else:
                st.session_state.pop(f"moving_stop_{trade.id}", None)
                st.success(
                    f"Moved {trade.ticker} stop to {_fmt_money(float(new_stop), 2)}"
                )
                st.rerun()
        elif cancel:
            st.session_state.pop(f"moving_stop_{trade.id}", None)
            st.rerun()


def _render_sell_shares_form(trade: Trade):
    """Form to record a SELL (partial or full) on an existing position."""
    with st.form(f"sell_shares_{trade.id}", clear_on_submit=True):
        st.markdown(f"**Sell shares of {trade.ticker}** (currently holding "
                    f"{_fmt_int(trade.net_shares)} shares, avg cost {_fmt_money(trade.avg_entry_price, 2)})")
        c1, c2, c3 = st.columns(3)
        with c1:
            sell_date = st.date_input("Date", value=date.today(), key=f"sell_dt_{trade.id}")
        with c2:
            sell_price = st.number_input(
                "Price per share ($)", min_value=0.0,
                value=float(trade.avg_entry_price), step=0.01, format="%.4f",
                key=f"sell_px_{trade.id}",
            )
        with c3:
            sell_shares_n = st.number_input(
                "Shares to sell", min_value=0,
                max_value=int(trade.net_shares),
                value=int(trade.net_shares),  # default = sell all (single-click close)
                step=1,
                key=f"sell_sh_{trade.id}",
                help=f"Max {trade.net_shares} (your current holding). "
                     "Leave at max to close the entire position.",
            )
        sell_note = st.text_area(
            "Add to notes (optional)",
            placeholder="e.g. taking 1/3 off at first measured-move target — letting rest run",
            height=70, key=f"sell_note_{trade.id}",
            help="Appended to the position's Notes section as a timestamped line.",
        )
        sub1, sub2 = st.columns(2)
        with sub1:
            do_sell = st.form_submit_button("Sell Shares", type="primary",
                                             use_container_width=True)
        with sub2:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

        if do_sell:
            if sell_price <= 0 or sell_shares_n <= 0:
                st.error("Price and shares must both be > 0")
            else:
                ok_msg = {"ok": True, "msg": ""}
                def _mutate(t):
                    ok, msg = t.add_sell(str(sell_date), sell_price, int(sell_shares_n))
                    ok_msg["ok"] = ok
                    ok_msg["msg"] = msg
                    if ok and sell_note and sell_note.strip():
                        action = f"SELL {int(sell_shares_n)} @ ${sell_price:,.2f}"
                        t.append_note(action, sell_note, str(sell_date))
                update_trade(trade.id, _mutate)
                if not ok_msg["ok"]:
                    st.error(ok_msg["msg"])
                else:
                    st.session_state.pop(f"selling_{trade.id}", None)
                    closed_note = " (position now CLOSED)" if int(sell_shares_n) >= trade.net_shares else ""
                    st.success(
                        f"Sold {_fmt_int(int(sell_shares_n))} shares of {trade.ticker} @ "
                        f"{_fmt_money(sell_price, 2)}{closed_note}"
                    )
                    st.rerun()
        elif cancel:
            st.session_state.pop(f"selling_{trade.id}", None)
            st.rerun()


# ---------------------------------------------------------------------------
# Per-row actions panel
# ---------------------------------------------------------------------------

def _render_trade_actions(trade: Trade):
    """Inline action panel — Add Shares / Sell Shares / Move Stop / Snapshot / Delete.

    Move Stop only renders when the position has held shares; closed positions
    can't trail a stop on nothing. Snapshot is always available — useful even
    on closed positions for post-hoc review."""
    is_adding   = st.session_state.get(f"adding_{trade.id}", False)
    is_selling  = st.session_state.get(f"selling_{trade.id}", False)
    is_moving   = st.session_state.get(f"moving_stop_{trade.id}", False)
    is_snapshot = st.session_state.get(f"showing_snapshot_{trade.id}", False)
    is_confirming_delete = st.session_state.get(f"confirm_del_{trade.id}", False)

    if is_adding:
        _render_add_shares_form(trade)
        return
    if is_selling:
        _render_sell_shares_form(trade)
        return
    if is_moving:
        _render_move_stop_form(trade)
        return
    if is_snapshot:
        _render_decision_snapshot(trade)
        return
    if is_confirming_delete:
        st.warning(f"Delete trade {trade.ticker} ({trade.id})? This removes ALL "
                   f"transactions in this position. Cannot be undone.")
        cdel, ccan = st.columns(2)
        with cdel:
            if st.button("Yes, delete", type="primary", key=f"del_yes_{trade.id}",
                         use_container_width=True):
                delete_trade(trade.id)
                st.session_state.pop(f"confirm_del_{trade.id}", None)
                st.success(f"Deleted {trade.ticker}")
                st.rerun()
        with ccan:
            if st.button("Cancel", key=f"del_no_{trade.id}", use_container_width=True):
                st.session_state.pop(f"confirm_del_{trade.id}", None)
                st.rerun()
        return

    c1, c2, c3, c4, c5, c6 = st.columns([1, 1, 1, 1, 1, 2])
    with c1:
        if trade.status != "closed":
            if st.button("➕ Add Shares", key=f"add_btn_{trade.id}",
                         type="primary", use_container_width=True):
                st.session_state[f"adding_{trade.id}"] = True
                st.rerun()
    with c2:
        if trade.net_shares > 0:
            if st.button("➖ Sell Shares", key=f"sell_btn_{trade.id}",
                         type="primary", use_container_width=True):
                st.session_state[f"selling_{trade.id}"] = True
                st.rerun()
    with c3:
        if trade.net_shares > 0:
            if st.button("🎯 Move Stop", key=f"stop_btn_{trade.id}",
                         type="primary", use_container_width=True,
                         help="Update the live stop. Initial-stop anchor for "
                              "R-multiple math stays unchanged."):
                st.session_state[f"moving_stop_{trade.id}"] = True
                st.rerun()
    with c4:
        if st.button("📊 Snapshot", key=f"snap_btn_{trade.id}",
                     type="primary", use_container_width=True,
                     help="Decision Snapshot — surfaces the metrics that should "
                          "inform a scale-out-vs-pyramid decision. Not a "
                          "recommendation; you make the call."):
            st.session_state[f"showing_snapshot_{trade.id}"] = True
            st.rerun()
    with c5:
        if st.button("🗑 Delete", key=f"del_btn_{trade.id}",
                     use_container_width=True):
            st.session_state[f"confirm_del_{trade.id}"] = True
            st.rerun()


# ---------------------------------------------------------------------------
# Transaction history detail
# ---------------------------------------------------------------------------

def _render_transaction_history(trade: Trade):
    """Render the full transaction list for a position."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]
    up = PALETTE["up_green"]
    down = PALETTE["down_red"]

    if not trade.transactions:
        st.caption("No transactions recorded.")
        return

    rows = []
    for i, tx in enumerate(trade.transactions, start=1):
        label = "STARTER" if (i == 1 and tx.kind == "BUY") else (
            "ADD" if tx.kind == "BUY" else "SELL"
        )
        rows.append({
            "#": i,
            "Type": label,
            "Date": _fmt_date(tx.date),
            "Price": _fmt_money(tx.price, 2),
            "Shares": _fmt_int(tx.shares),
            "Notional": _fmt_money(tx.price * tx.shares, 0),
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Position totals — show BOTH initial risk (R-multiple anchor) AND live
    # risk (current exposure). Initial is frozen; live updates as you trail.
    r_str = f"{trade.realized_r:+.2f}R" if trade.realized_r is not None else "— (no stop)"
    rp_initial = _risk_pct(trade)
    if trade.initial_risk_dollars > 0:
        rp_str = f" ({rp_initial:.1f}%)" if rp_initial is not None else ""
        risk_str = f"{_fmt_money(trade.initial_risk_dollars, 0)}{rp_str}"
    else:
        risk_str = "no stop set"
    live_rd = _live_risk_dollars(trade)
    live_rp = _live_risk_pct(trade)
    if live_rd is not None:
        live_color = up if live_rd <= 0 else cyan
        live_rp_str = f" ({live_rp:+.1f}%)" if live_rp is not None else ""
        protected_tag = " <b style='color:" + up + "'>— PROTECTED</b>" if live_rd <= 0 else ""
        live_risk_line = (
            f"&nbsp;&nbsp;Live risk:     "
            f"<b style='color:{live_color}'>{_fmt_money(live_rd, 0, signed=True)}</b>"
            f"{live_rp_str}{protected_tag}"
        )
    else:
        live_risk_line = (
            f"&nbsp;&nbsp;Live risk:     "
            f"<span style='color:{muted}'>—</span>"
        )
    # Show current vs initial stop side-by-side so the trailing journey is visible
    cur_stop_str = (_fmt_money(trade.stop_price, 2)
                    if trade.stop_price > 0 else "—")
    init_stop_str = (_fmt_money(trade.initial_stop_price, 2)
                     if trade.initial_stop_price > 0 else "—")
    stop_moved = (trade.initial_stop_price > 0
                  and trade.stop_price > 0
                  and abs(trade.stop_price - trade.initial_stop_price) > 0.001)
    stop_line = (
        f"&nbsp;&nbsp;Stop: <span style='color:{cyan}'>{cur_stop_str}</span> (current)"
    )
    if stop_moved:
        stop_line += f" · initial {init_stop_str}"
    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem;line-height:1.7;"
        f"margin-top:0.5rem'>"
        f"<b>Position totals:</b><br>"
        f"&nbsp;&nbsp;Total bought: <b>{_fmt_int(trade.total_shares_bought)}</b> shares · "
        f"avg entry <span style='color:{cyan}'>{_fmt_money(trade.avg_entry_price, 2)}</span><br>"
        f"&nbsp;&nbsp;Total sold:   <b>{_fmt_int(trade.total_shares_sold)}</b> shares · "
        f"avg exit  <span style='color:{cyan}'>{_fmt_money(trade.avg_exit_price, 2)}</span><br>"
        f"&nbsp;&nbsp;Currently holding: <b>{_fmt_int(trade.net_shares)}</b> shares "
        f"(cost basis {_fmt_money(trade.cost_basis, 0)})<br>"
        f"{stop_line}<br>"
        f"&nbsp;&nbsp;Initial risk:  {risk_str} <span style='color:{muted}'>(R-multiple anchor — frozen at entry, does not change when you trail the stop)</span><br>"
        f"{live_risk_line} <span style='color:{muted}'>(current exposure if stopped today — updates with stop trails / adds / scales)</span><br>"
        f"&nbsp;&nbsp;Realized P&L: <b style='color:{up if trade.realized_pnl > 0 else down}'>"
        f"{_fmt_money(trade.realized_pnl, 0, signed=True)}</b> · R-multiple: <b>{r_str}</b>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Stop history table — only shown when the user has trailed the stop
    if trade.stop_history:
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem;"
            f"font-weight:700;color:{cyan};margin-top:0.8rem;margin-bottom:0.2rem'>"
            f"Stop history</div>",
            unsafe_allow_html=True,
        )
        sh_rows = []
        for i, su in enumerate(trade.stop_history, start=1):
            sh_rows.append({
                "#": i,
                "Date": _fmt_date(su.date),
                "From": _fmt_money(su.old_stop, 2) if su.old_stop > 0 else "—",
                "To": _fmt_money(su.new_stop, 2),
                "Δ": _fmt_money(su.new_stop - su.old_stop, 2, signed=True) if su.old_stop > 0 else "—",
                "Note": su.note or "",
            })
        st.dataframe(pd.DataFrame(sh_rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def _trades_to_dataframe(trades: list[Trade]) -> pd.DataFrame:
    """Convert list of Trade dataclasses into a display dataframe — one row per position."""
    rows = []
    for t in trades:
        r_str = f"{t.realized_r:+.2f}" if t.realized_r is not None else "—"
        # Live risk (current exposure) — shown in the Risk $/Risk % columns.
        # Initial-risk values (frozen R-multiple anchor) live in the per-row
        # detail panel only.
        live_risk_d = _live_risk_dollars(t)
        live_risk_p = _live_risk_pct(t)
        # Last close + its date from the local price cache. Blank for closed
        # positions (no held shares — last close isn't decision-relevant).
        last_result = _last_close_for(t.ticker) if t.net_shares > 0 else None
        last_price = last_result[0] if last_result else None
        last_date  = last_result[1] if last_result else ""
        # Total P&L = realized + unrealized for held shares. For closed
        # positions this collapses to realized.
        pnl = _open_pnl(t, last_price)
        # Gain/Loss % = P&L $ as a % of total capital invested. Same denominator
        # for open/partial/closed positions so the metric is comparable.
        gl_pct = _gain_loss_pct(t, pnl)
        rows.append({
            "id": t.id,
            "Ticker": t.ticker,
            "Setup": t.setup_tag,
            "Status": t.status.upper(),
            # Dates rendered as 11Jun2026; keep raw "Closed_Raw" for the sort key
            "Opened": _fmt_date(t.entry_date),
            "Closed": _fmt_date(t.last_exit_date) if t.status == "closed" else "",
            "Closed_Raw": t.last_exit_date if t.status == "closed" else "",
            "Held": t.net_shares,
            "Avg Entry": t.avg_entry_price,
            # Cost $ = currently-held shares × weighted-avg entry price.
            # For CLOSED positions this is 0 (no held shares); for OPEN/PARTIAL
            # it's the dollar value of the position at cost basis.
            "Cost $": t.cost_basis,
            "Last $": last_price if last_price is not None else 0.0,
            "Last Date": last_date,
            "Avg Exit": t.avg_exit_price if t.total_shares_sold > 0 else 0.0,
            "Stop": t.stop_price if t.stop_price > 0 else 0.0,
            # Risk $/Risk % show CURRENT exposure (live). Initial-risk
            # anchor (used by R-multiple) is displayed in the row detail panel.
            "Risk $": live_risk_d if live_risk_d is not None else 0.0,
            "Risk %": live_risk_p if live_risk_p is not None else None,
            "Realized $": t.realized_pnl,
            "P&L $": pnl if pnl is not None else 0.0,
            "G/L %": gl_pct if gl_pct is not None else None,
            "R": r_str,
            "Notes": t.notes,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        # Sort: open first, then partial, then closed (most recent closed at top of closed).
        # Closed_Raw still has YYYY-MM-DD so pd.Timestamp parses it cleanly.
        status_rank = {"OPEN": 0, "PARTIAL": 1, "CLOSED": 2}
        df["_sort_key"] = df.apply(
            lambda r: (status_rank.get(r["Status"], 3),
                       -pd.Timestamp(r["Closed_Raw"]).timestamp()
                       if r["Status"] == "CLOSED" and r["Closed_Raw"] else 0),
            axis=1,
        )
        df = df.sort_values("_sort_key").drop(columns=["_sort_key", "Closed_Raw"]).reset_index(drop=True)
    return df


def _render_trades_table(trades: list[Trade]):
    """Render the trades table + per-row expander with actions + transaction history."""
    if not trades:
        st.info(
            "No trades logged yet. Click **➕ Log a new trade** above to add your first one.\n\n"
            "After 30-50 closed positions with stops, the expectancy-by-setup breakdown "
            "becomes statistically meaningful."
        )
        return

    df = _trades_to_dataframe(trades)
    display = df.drop(columns=["id", "Notes"])

    # All money formats use comma thousands separator (e.g. $1,200 not $1200).
    # Risk % displays as "5.4%" (per-share stop distance as % of entry).
    # Last $ + P&L $ added 2026-06-11 — last close pulled from the local price
    # cache, P&L = realized + (last - avg_entry) × held shares.
    column_config = {
        "Ticker":     st.column_config.TextColumn("Ticker", pinned=True, width="small"),
        "Setup":      st.column_config.TextColumn("Setup", width="small"),
        "Status":     st.column_config.TextColumn("Status", width="small", alignment="center"),
        "Opened":     st.column_config.TextColumn("Opened", width="small"),
        "Closed":     st.column_config.TextColumn("Closed", width="small"),
        "Held":       st.column_config.NumberColumn("Held", format="%,d", width="small", alignment="center"),
        "Avg Entry":  st.column_config.NumberColumn("Avg Entry", format="$%,.2f", width="small", alignment="center"),
        "Cost $":     st.column_config.NumberColumn(
            "Cost $", format="$%,.0f", width="small", alignment="center",
            help="Cost basis of currently-held shares: Held × Avg Entry. "
                 "Position size in dollars at cost. 0 for closed positions.",
        ),
        "Last $":     st.column_config.NumberColumn(
            "Last $", format="$%,.2f", width="small", alignment="center",
            help="Most recent close from the local price cache (populated by every full scan or "
                 "the Refresh Prices sidebar button). Blank/0 for closed positions and any "
                 "ticker that hasn't been in a scan yet.",
        ),
        "Last Date":  st.column_config.TextColumn(
            "Last Date", width="small", alignment="center",
            help="Date of the cached close shown in Last $. If this is older than yesterday, "
                 "your data is stale — click the Refresh Prices button in the sidebar to fetch fresh closes.",
        ),
        "Avg Exit":   st.column_config.NumberColumn("Avg Exit", format="$%,.2f", width="small", alignment="center"),
        "Stop":       st.column_config.NumberColumn("Stop", format="$%,.2f", width="small", alignment="center"),
        "Risk $":     st.column_config.NumberColumn(
            "Risk $", format="$%,.0f", width="small", alignment="center",
            help="LIVE exposure if stopped right now: (avg_entry − current_stop) × held_shares. "
                 "Updates as you trail stops, add shares, or scale out. Green = NEGATIVE "
                 "(stop above avg entry — locked-in profit, can't take a loss from here). "
                 "Initial-commitment risk (R-multiple anchor) is shown in the per-row detail panel.",
        ),
        "Risk %":     st.column_config.NumberColumn(
            "Risk %", format="%.1f%%", width="small", alignment="center",
            help="LIVE stop distance from current avg entry as %: "
                 "(avg_entry − current_stop) / avg_entry × 100. "
                 "Negative when stop is above avg entry (locked-in profit).",
        ),
        "Realized $": st.column_config.NumberColumn("Realized $", format="$%+,.0f", width="small", alignment="center"),
        "P&L $":      st.column_config.NumberColumn(
            "P&L $", format="$%+,.0f", width="small", alignment="center",
            help="Total P&L on the position right now: Realized + (Last − Avg Entry) × Held shares. "
                 "For CLOSED positions this equals Realized. For OPEN/PARTIAL positions it includes "
                 "the unrealized swing on currently-held shares using the last cached close.",
        ),
        "G/L %":      st.column_config.NumberColumn(
            "G/L %", format="%+.1f%%", width="small", alignment="center",
            help="Gain/Loss as % of total capital invested: P&L $ ÷ (Avg Entry × Total Shares Bought) × 100. "
                 "Return on capital across the full life of the position — comparable across open/partial/closed.",
        ),
        "R":          st.column_config.TextColumn(
            "R-Multiple", width="small", alignment="center",
            help="Realized R-multiple: Realized $ ÷ Initial Risk $. "
                 "A +2 means the trade returned 2× your initial risk; -1 means you lost the full risked amount. "
                 "Shows '—' when no stop was set (R can't compute without a risk basis).",
        ),
    }

    # Apply two coloring rules:
    #   - Red-on-negative for P&L-bearing columns (loss visualization)
    #   - Green-on-non-positive for live Risk columns (protected visualization)
    # Streamlit composes both styler rules on top of the column_config formats.
    # Use .map() (pandas 2.1+); fall back to .applymap() for older pandas.
    pnl_cols  = [c for c in ("Realized $", "P&L $", "G/L %", "R") if c in display.columns]
    risk_cols = [c for c in ("Risk $", "Risk %") if c in display.columns]

    def _apply(styler, fn, subset):
        try:
            return styler.map(fn, subset=subset)
        except AttributeError:
            return styler.applymap(fn, subset=subset)  # pandas < 2.1 fallback

    styled = display.style
    if pnl_cols:
        styled = _apply(styled, _negative_red, pnl_cols)
    if risk_cols:
        styled = _apply(styled, _protected_green, risk_cols)

    selection = st.dataframe(
        styled,
        use_container_width=True,
        height=min(640, 90 + len(display) * 36),
        hide_index=True,
        column_config=column_config,
        on_select="rerun",
        selection_mode="single-row",
        key="journal_table",
    )

    # Aggregate totals for held positions (sum of P&L on open holdings).
    # Renders right below the table so it's always visible, regardless of
    # whether a row is selected for the detail expander below.
    _render_held_totals(trades)

    # On row click — show transaction history + actions for that trade
    if selection and "selection" in selection and selection["selection"].get("rows"):
        idx = selection["selection"]["rows"][0]
        trade_id = df.iloc[idx]["id"]
        trade = next((t for t in trades if t.id == trade_id), None)
        if trade is None:
            return

        st.markdown("---")
        cyan = PALETTE["accent_cyan"]
        muted = PALETTE["text_muted"]
        body = (
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{muted}'>"
            f"ID: <span style='color:{cyan}'>{trade.id}</span> &nbsp;|&nbsp; "
            f"Created: {_fmt_date(trade.created_at) if trade.created_at else '—'}"
            f"</div>"
        )
        st.markdown(panel(f"{trade.ticker} — {trade.setup_tag} — {trade.status.upper()}", body),
                    unsafe_allow_html=True)

        if trade.notes:
            # white-space: pre-wrap preserves the \n line breaks that
            # append_note inserts between timestamped action entries, while
            # still wrapping long lines naturally. The dark panel + monospace
            # font keep the running log visually distinct from chart prose.
            import html as _html
            notes_html = _html.escape(trade.notes)
            st.markdown(
                f"<div style='font-family:JetBrains Mono;font-size:0.82rem;"
                f"white-space:pre-wrap;background:#0F1620;padding:0.6rem 0.8rem;"
                f"border-radius:4px;border:1px solid #1F2937;margin-top:0.3rem;"
                f"line-height:1.55'>"
                f"<b style='color:{cyan}'>Notes</b>\n{notes_html}"
                f"</div>",
                unsafe_allow_html=True,
            )

        # Transaction history table
        st.markdown("##### Transactions")
        _render_transaction_history(trade)

        # Action buttons (Add / Sell / Delete) below the history
        st.markdown("")
        _render_trade_actions(trade)


# ---------------------------------------------------------------------------
# Expectancy-by-setup table (collapsed by default)
# ---------------------------------------------------------------------------

def _render_expectancy_breakdown(trades: list[Trade]):
    """Per-setup expectancy table — closed trades with computable R only."""
    stats = expectancy_by_setup(trades)
    with st.expander("📊 Expectancy by setup (closed trades with stops only)", expanded=False):
        if not stats:
            st.info("No closed trades with computable R yet. Close some open positions "
                    "(with stop prices set) to populate this table.")
            return
        rows = []
        for tag, s in sorted(stats.items(), key=lambda kv: -kv[1]["expectancy_r"]):
            rows.append({
                "Setup":         tag,
                "N":             s["n"],
                "Wins":          s["wins"],
                "Losses":        s["losses"],
                "Hit Rate":      f"{s['hit_rate']*100:.0f}%",
                "Avg Win (R)":   f"{s['avg_win_r']:+.2f}",
                "Avg Loss (R)":  f"{s['avg_loss_r']:+.2f}",
                "Expectancy (R)":f"{s['expectancy_r']:+.2f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "Expectancy = (hit rate × avg win R) + (loss rate × avg loss R). "
            "Positive = setup pays you on average. Sample size matters — "
            "decisions based on <20 trades per setup are noise."
        )


# ---------------------------------------------------------------------------
# Table of Contents dialog (rationale + column/feature definitions)
# ---------------------------------------------------------------------------

@st.dialog("Trade Journal — Table of Contents", width="large")
def _journal_toc_dialog():
    """Modal dialog explaining the journal's purpose, model, columns, actions,
    and expectancy math. Same pattern as Quality / Momentum TOC dialogs."""

    st.markdown("### Why this tab exists")
    st.markdown(
        "You can't improve a process you aren't measuring. When you take a trade, "
        "the chain of decisions disappears into memory — what setup you saw, where "
        "your stop was, why you added or sold, whether the trade actually paid you. "
        "Without that record, you have no honest way to answer **\"do VCP-A setups "
        "actually pay me?\"** or **\"is my position sizing right given my edge?\"** "
        "You're guessing — while convinced you're learning."
    )
    st.markdown(
        "The journal turns each trade into one row of data. After 30-50 closed "
        "positions per setup category, the **expectancy** number becomes meaningful "
        "— it tells you, in average R-multiples, whether a given setup pays you, "
        "breaks even, or bleeds money. That's the foundation for sizing up on what "
        "works, dropping what doesn't, and ultimately calling yourself a professional "
        "trader honestly (rather than aspirationally)."
    )

    st.markdown("---")
    st.markdown("### The position model")
    st.markdown(
        "**One row = one position = one thesis.** Each row represents a single "
        "trade idea (e.g. \"NVDA on a VCP-A breakout from $400\"). Inside that "
        "row, you log every buy and sell — the starter entry, any pyramid adds, "
        "partial profit takes, and the final exit. The journal does the math: "
        "weighted-average entry, weighted-average exit, total realized P&L, and "
        "R-multiple anchored to your initial risk."
    )
    st.markdown(
        "**Click any row** to see its full transaction history, stop history, "
        "and the per-row action buttons:\n"
        "- **➕ Add Shares** — log a pyramid add (new BUY transaction)\n"
        "- **➖ Sell Shares** — log a partial exit or close (new SELL transaction)\n"
        "- **🎯 Move Stop** — trail your stop higher as the trade progresses "
        "(the initial-stop anchor for R-multiple math doesn't change — see Stop tracking below)\n"
        "- **🗑 Delete** — remove the whole position (with confirmation)"
    )

    st.markdown("---")
    st.markdown("### Stop tracking — initial vs current")
    st.markdown(
        "The journal tracks **two** stops per position:\n\n"
        "- **Initial stop** (`initial_stop_price`) — set once at trade entry, "
        "never changes. This is what `Risk $` / `Risk %` / `R-Multiple` are "
        "anchored to. The whole point: if you raise your stop to lock in "
        "profit, you don't want the risk basis to shrink retroactively — that "
        "would inflate every winning trade's R-multiple to absurd levels and "
        "ruin the expectancy math.\n\n"
        "- **Current stop** (`stop_price`, the one shown in the **Stop** column) — "
        "the live trailing stop. Update it via **🎯 Move Stop** whenever you "
        "tighten. Each change is logged with date + old/new value + optional note, "
        "and you can see the full progression in the **Stop history** table that "
        "appears in the row's detail area.\n\n"
        "**Why this matters for Minervini-style trading:** the discipline of "
        "progressive stop-tightening (move to breakeven once you're +10-15%, "
        "trail under successive higher lows, lock in 50% gains at the right "
        "moment) is half of what separates winners from losers. The journal "
        "lets you replay your stop decisions later and ask: did I tighten too "
        "early? Too late? Did I give back too much profit before the trail caught?"
    )

    st.markdown("---")
    st.markdown("### Table columns")
    cols_docs = {
        "Ticker":     "Stock symbol (uppercased).",
        "Setup":      "The setup tag chosen at entry. Drives the expectancy-by-setup grouping.",
        "Status":     "OPEN = nothing sold yet · PARTIAL = some sold, some still held · CLOSED = fully exited.",
        "Opened":     "Date of the starter (first) BUY transaction.",
        "Closed":     "Date of the final SELL (blank for OPEN/PARTIAL).",
        "Held":       "Net shares currently held (total bought − total sold).",
        "Avg Entry":  "Weighted-average price across all BUY transactions.",
        "Cost $":     "Cost basis of currently-held shares (Held × Avg Entry). Position size in dollars at cost. 0 for closed positions.",
        "Last $":     "Most recent close from the local price cache (populated by every full scan or the Refresh Prices sidebar button). Blank/0 for closed positions or tickers that haven't been in a scan yet.",
        "Last Date":  "Date of the cached close shown in Last $. If older than yesterday, your data is stale — use the Refresh Prices button in the sidebar.",
        "Avg Exit":   "Weighted-average price across all SELL transactions (0 if nothing sold).",
        "Stop":       "Initial stop price. 0 means no stop was set — R-multiple won't compute.",
        "Risk $":     "Initial dollar risk: (starter entry − stop) × starter shares. 0 if no stop set.",
        "Risk %":     "Stop distance as % of starter entry: (entry − stop) / entry × 100. Minervini caps at ~7-8% max; tighter (3-5%) is preferred on clean VCPs.",
        "Realized $": "Realized P&L using weighted-average-cost (proceeds − avg_entry × shares_sold).",
        "P&L $":      "Total P&L right now: Realized + (Last − Avg Entry) × Held. For CLOSED positions equals Realized. For OPEN/PARTIAL adds the unrealized swing on currently-held shares.",
        "G/L %":      "Gain/Loss as % of total capital invested: P&L $ ÷ (Avg Entry × Total Shares Bought) × 100. Return on capital comparable across open/partial/closed positions.",
        "R-Multiple": "Realized R-multiple: Realized $ ÷ Initial Risk $. +2 means the trade returned 2× your initial risk; −1 means you lost the full risked amount. Shows \"—\" if no stop was set.",
    }
    for col, doc in cols_docs.items():
        st.markdown(f"- **{col}** — {doc}")

    st.markdown("---")
    st.markdown("### Summary tiles (top of page)")
    tile_docs = {
        "OPEN POSITIONS": "Positions still holding shares (OPEN + PARTIAL combined). The partial count is shown underneath.",
        "CLOSED":         "Fully exited positions.",
        "WIN RATE":       "Of closed positions WITH a stop set, the percentage that returned a positive R-multiple.",
        "EXPECTANCY (R)": "Average R-multiple per rated trade = (hit_rate × avg_win_R) + (loss_rate × avg_loss_R). Positive = setup pays on average.",
        "REALIZED P&L":   "Total realized dollars across all closed + partial positions (does NOT include unrealized).",
    }
    for tile, doc in tile_docs.items():
        st.markdown(f"- **{tile}** — {doc}")

    st.markdown("---")
    st.markdown("### Setup tags — when to use which")
    tag_docs = {
        "VCP-A":                "Textbook Minervini Volatility Contraction Pattern — multiple shrinking pullbacks, current contraction <6%, volume drying up on each successive low.",
        "VCP-B":                "Looser VCP — 2+ contractions, final pullback <10%. Not as clean as A but still tightening.",
        "Breakout":             "Stock just cleared a defined resistance/pivot on volume. Not necessarily a VCP — could be a flat base, cup-and-handle, etc.",
        "Power Play":           "Minervini's high-tight setup: +100% in 8 weeks then consolidating <25% off the high. Rare and powerful.",
        "Pullback to 50MA":     "Stock in confirmed Stage 2 uptrend pulls back to the 50-day MA on light volume — buyable bounce setup.",
        "Coil-in-progress":     "Pattern is forming (multiple contractions visible) but hasn't broken out yet. EARLY entry, more risk.",
        "RS Divergence":        "Relative strength line at new high while price still inside the base — institutional accumulation signal before the obvious breakout.",
        "Stage 1->2 Transition":"Long flat base ends; 150-day MA flattens then turns up. The earliest legitimate entry in the whole stage cycle.",
        "Earnings Inflection":  "Latest quarter's earnings growth materially accelerated vs the prior 4 quarters — fundamental catalyst before the multiple re-rates.",
        "Other":                "Catch-all. Use sparingly — if you keep using Other, that's signal to add a new tag.",
    }
    for tag, doc in tag_docs.items():
        st.markdown(f"- **{tag}** — {doc}")

    st.markdown("---")
    st.markdown("### Expectancy math")
    st.markdown(
        "**R-multiple** is the natural unit for measuring trade outcomes: how many "
        "multiples of your initial risk did the trade return? If you risked $100 "
        "and made $300, that's +3R. If you lost the full risked amount, that's −1R."
    )
    st.markdown(
        "**Expectancy** = (hit rate × average winning R) + (loss rate × average losing R)\n\n"
        "Example: 40% win rate, average win +2.5R, average loss −1.0R\n"
        "    → Expectancy = (0.40 × 2.5) + (0.60 × −1.0) = +0.40R per trade\n\n"
        "Positive expectancy = the setup pays you on average. The whole point of the "
        "journal is to compute this number per setup so you can size up the positive "
        "ones and drop the negative ones."
    )
    st.markdown(
        "**Sample size caveat:** decisions based on fewer than ~20 closed trades "
        "per setup are mostly noise. Aim for 30-50 before you trust the number "
        "enough to change your behavior."
    )

    st.markdown("---")
    st.markdown("### Optional stop — when and why")
    st.markdown(
        "The journal lets you save trades without a stop price (leave Stop at 0). "
        "P&L still computes; only the R-multiple doesn't. Use this for thesis-driven "
        "holds where you'll exit on discretion (earnings inflection plays, multi-month "
        "swing positions, etc.). The trade-off: those trades won't contribute to the "
        "expectancy-by-setup calculation, because there's no risk anchor to compute "
        "R against."
    )

    if st.button("Close", use_container_width=True, key="journal_toc_close"):
        st.rerun()


# ---------------------------------------------------------------------------
# Page + sidebar
# ---------------------------------------------------------------------------

def render_journal_page():
    """Main journal page — summary tiles + add-trade form + table + expectancy."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:0.85rem;color:{muted};"
        f"padding-top:0.5rem'>JOURNAL  /  Position log + expectancy</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<h2 style='margin-top:0.6rem'>📓 Trade Journal</h2>"
        f"<div style='color:{muted};font-size:0.9rem;line-height:1.5;margin-bottom:1rem;max-width:900px'>"
        f"One row = one position. Click any row to see its full transaction history "
        f"and add more shares, sell some, or close it out. After 30-50 closed positions "
        f"with stops, the expectancy-by-setup breakdown shows you which patterns actually "
        f"pay you — the foundation for sizing up on what works."
        f"</div>",
        unsafe_allow_html=True,
    )

    # "About this page" expander — rationale up front, collapsible once read.
    # Full TOC dialog (sidebar button) has the deeper detail.
    with st.expander("ℹ️ Why this page exists (click for the rationale)", expanded=False):
        st.markdown(
            "**You can't improve a process you aren't measuring.** When you take a "
            "trade, the chain of decisions disappears into memory — what setup you "
            "saw, where your stop was, why you added or sold, whether the trade "
            "actually paid you. Without that record, you have no honest way to "
            "answer *\"do VCP-A setups actually pay me?\"* or *\"is my position "
            "sizing right given my edge?\"* You're guessing — while convinced "
            "you're learning."
        )
        st.markdown(
            "The journal turns each trade into one row of data. After 30-50 closed "
            "positions per setup category, the **expectancy** number becomes "
            "meaningful — it tells you, in average R-multiples, whether a setup pays "
            "you, breaks even, or bleeds money. That's the foundation for sizing up "
            "on what works, dropping what doesn't, and ultimately calling yourself "
            "a professional trader honestly."
        )
        st.markdown(
            "**The position model** — one row contains all the buys and sells of a "
            "single thesis — exists because real trades don't look like *\"buy 100, "
            "sell 100.\"* They look like *\"buy 50 starter, add 50 on confirmation, "
            "sell 25 at first target, trail the rest.\"* The journal captures that "
            "reality so the expectancy math reflects what you actually did."
        )
        st.markdown(
            "Full column/feature definitions are in the **📖 Table of Contents** "
            "button in the sidebar."
        )

    trades = load_trades()
    _render_summary_tiles(trades)
    _render_add_form()
    st.markdown("")
    _render_trades_table(trades)
    st.markdown("")
    _render_expectancy_breakdown(trades)


def render_journal_sidebar():
    """Journal sidebar — title + summary + Excel export."""
    cyan = PALETTE["accent_cyan"]
    muted = PALETTE["text_muted"]

    st.markdown(
        f"<div style='font-family:JetBrains Mono;font-size:1.3rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem;line-height:1.2'>📓 LEGACY BRANDS</div>"
        f"<div style='font-family:JetBrains Mono;font-size:0.8rem;font-weight:700;"
        f"color:{cyan};margin-bottom:0.5rem'>TRADE JOURNAL</div>"
        f"<div style='color:{muted};font-size:0.75rem;margin-bottom:1.0rem'>"
        f"Log positions. Pyramid + scale out. Measure expectancy.</div>",
        unsafe_allow_html=True,
    )

    trades = load_trades()
    open_n = sum(1 for t in trades if t.status == "open")
    partial_n = sum(1 for t in trades if t.status == "partial")
    closed_n = sum(1 for t in trades if t.status == "closed")
    held_count = sum(1 for t in trades if t.net_shares > 0)

    # ---- Refresh Prices button (top action — refreshes Last $ / P&L $) ----
    # Reads only the tickers in held positions, not the full price cache.
    # Typical journal of 5-30 tickers refreshes in 10-30 seconds.
    if held_count > 0:
        if st.button(
            f"🔄 Refresh Prices ({held_count})",
            type="primary", use_container_width=True,
            key="journal_refresh_prices",
            help=f"Fetch the latest closing prices from yfinance for the "
                 f"{held_count} ticker(s) you currently hold. Updates the Last $ "
                 f"and P&L $ columns. Takes ~10-30 sec.",
        ):
            succ, fail = _refresh_journal_prices(trades)
            if succ > 0 and fail == 0:
                st.success(f"Refreshed {succ} ticker(s)")
            elif succ > 0 and fail > 0:
                st.warning(f"Refreshed {succ}, failed {fail}")
            else:
                st.error(f"No tickers refreshed ({fail} failures — check network/yfinance)")
            st.rerun()
    else:
        st.caption("No held positions — nothing to refresh.")

    st.markdown("---")

    st.markdown(
        panel(
            "AT A GLANCE",
            f"<div style='font-family:JetBrains Mono;font-size:0.85rem'>"
            f"Total positions: <b>{len(trades)}</b><br>"
            f"Open: <span style='color:{cyan}'>{open_n}</span><br>"
            f"Partial: <span style='color:{cyan}'>{partial_n}</span><br>"
            f"Closed: <span style='color:{cyan}'>{closed_n}</span></div>"
        ),
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # Excel export — exports the position-level table (one row per position)
    if trades:
        try:
            df = _trades_to_dataframe(trades).drop(columns=["id"])
            stamp = datetime.utcnow().strftime("%d%b%Y")
            excel_bytes = format_dataframe_to_excel(
                df,
                sheet_name="Trade Journal",
                title=f"Legacy Brands - Trade Journal {stamp}",
            )
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"trade_journal_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        except ModuleNotFoundError:
            st.caption("⚠ Excel export needs `openpyxl`. Run `pip install openpyxl` in the venv.")
        except Exception as e:
            st.caption(f"Excel export error: {e}")
    else:
        st.caption("No trades to export yet.")

    # Table of Contents button — opens the full reference dialog
    if st.button("📖 Table of Contents", type="primary", use_container_width=True,
                 key="journal_toc_btn",
                 help="Why this page exists, position model, column definitions, "
                      "per-row actions, expectancy math, setup tag meanings."):
        _journal_toc_dialog()

    st.markdown("---")
    st.markdown(
        f"<div style='color:{muted};font-size:0.75rem;line-height:1.5'>"
        f"<b>How it works:</b><br>"
        f"• One row = one position (one ticker × one thesis)<br>"
        f"• Click a row to see transactions + add/sell shares<br>"
        f"• Stop is optional — R only computes if stop is set<br>"
        f"• Expectancy uses weighted-avg-cost for partial exits"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown(
        f"<div style='color:{muted};font-size:0.75rem;line-height:1.5'>"
        f"<b>Setup tags:</b><br>"
        + "<br>".join(f"• {t}" for t in SETUP_TAGS) +
        f"</div>",
        unsafe_allow_html=True,
    )
