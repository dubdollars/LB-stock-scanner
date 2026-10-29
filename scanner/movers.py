"""
MOVERS scan — reverse funnel (added 2026-08-07).

Design philosophy
-----------------
The Momentum scan starts from STRUCTURE (base/VCP/RS character) and hopes
demand shows up on the breakout day. MOVERS inverts this: start from
CONFIRMED DEMAND today (real gain on real volume) and show the structure
context alongside. This is how you find the leaders the tape is picking,
not the ones you'd prefer it to pick.

Criteria (all four required)
----------------------------
  1. Today's close >= prior close * 1.03      (real move, not noise)
  2. Today's volume >= 1.5 * robust median    (real institutional footprint)
  3. Trend template intact:
       - price above 50MA, 150MA, 200MA
       - 50MA >= 150MA >= 200MA
       - 200MA rising (higher than 21 sessions ago)
       - RS rank >= 70
     (Minervini stage-2 gate — no falling knives)
  4. Close in upper half of today's range     (held the move to the bell)

Ranking
-------
Composite move score = gain_pct * vol_ratio. Rewards names doing BOTH
things well — a 3% move on 10x volume outscores a 15% move on 1.6x
volume (because the volume tells you it's a real accumulation event
that the crowd hasn't fully noticed yet, versus a runaway move that's
already been chased).

Output
------
Each entry carries the demand-side numbers PLUS enough structure context
to decide whether to trade it: RS, VCP grade, industry rank, whether
it's also in the momentum-scan BUY ZONE. This is intentionally NOT gated
on the industry rank filter — the whole point is to see leadership that
your normal filter might exclude.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .setups import calc_atr, robust_avg_volume


# ---------------------------------------------------------------------------
# Gate constants — kept as module-level for easy tuning + TOC documentation
# ---------------------------------------------------------------------------
MIN_GAIN_PCT       = 3.0    # today's close vs prior close
MIN_VOL_RATIO      = 1.5    # today's vol / robust 50d median
MIN_CLOSE_POS      = 0.5    # close in upper half of range (held the move)
MIN_RS_RANK        = 70     # Minervini floor
MIN_HISTORY_BARS   = 210    # need enough for 200MA + trend check

# Tradeable-liquidity floor — SAME threshold as BUY ZONE's ADV gate (see
# scanner/core.py:_MIN_BUY_ZONE_DOLLAR_VOLUME). A mover below this is
# essentially untradeable at $10k-$1M account sizes: 6-figure daily notional
# means your entry moves the tape and your exit blows the spread. Kept as
# a display filter (not a hard cut) so we can still show the raw mover pool
# for cross-reference, but default the toggle to hide them. Added 2026-08-07
# per user feedback that DCTH-class names were dominating the top ranks.
TRADEABLE_DOLLAR_ADV = 20_000_000


def _trend_template_ok(df: pd.DataFrame) -> bool:
    """Minervini stage-2 trend template — no falling knives.

    Six checks:
      1. price > 50MA
      2. price > 150MA
      3. price > 200MA
      4. 50MA > 150MA >= 200MA (stacked)
      5. 200MA rising (higher than 21 sessions ago)
      6. price >= 75% of 52-week high (has actually run)
    """
    try:
        if len(df) < MIN_HISTORY_BARS:
            return False
        close = df["Close"]
        sma50 = float(close.rolling(50).mean().iloc[-1])
        sma150 = float(close.rolling(150).mean().iloc[-1])
        sma200_now = float(close.rolling(200).mean().iloc[-1])
        sma200_21 = float(close.rolling(200).mean().iloc[-22])
        price = float(close.iloc[-1])
        hi_52w = float(close.tail(252).max())
        if price <= 0 or sma50 <= 0 or sma150 <= 0 or sma200_now <= 0:
            return False
        return (
            price > sma50
            and price > sma150
            and price > sma200_now
            and sma50 > sma150
            and sma150 >= sma200_now
            and sma200_now > sma200_21
            and price >= hi_52w * 0.75
        )
    except Exception:
        return False


def _score_mover(gain_pct: float, vol_ratio: float) -> float:
    """Raw composite ranking score. Higher = stronger move.

    Formula: gain_pct × vol_ratio. Structurally overweights micro-caps
    (a 20% squeeze on 5x vol crushes a 5% mid-cap accumulation on 3x vol
    even though the mid-cap is often the more actionable setup). Use in
    combination with Quality_Score for a balanced view.
    """
    return round(gain_pct * vol_ratio, 2)


def _quality_score(gain_pct: float, vol_ratio: float, atr_pct: float) -> float:
    """Volatility-normalized ranking score. Higher = stronger, cleaner move.

    Formula: (gain_pct / atr_pct) × vol_ratio.

    The gain-over-ATR ratio converts a raw percentage move into "how many
    typical daily ranges did this stock cover today?" — which makes moves
    comparable across volatility regimes. A $50 stock with $2 ATR (4% ATR)
    that gaps 6% moved 1.5 ATRs; a $5 stock with $1 ATR (20% ATR) that
    gaps 20% only moved 1.0 ATR. The mid-cap deserves the higher rank
    under this framework.

    Vol_ratio stays in the numerator because volume expansion IS the
    demand-confirmation signal — normalizing by volatility doesn't change
    the fact that we want real institutional flow.

    Falls back to raw _score_mover if ATR is missing / zero — better to
    show a less-normalized score than to drop the row entirely.
    """
    if atr_pct is None or atr_pct <= 0:
        return _score_mover(gain_pct, vol_ratio)
    return round((gain_pct / atr_pct) * vol_ratio, 2)


def find_movers(
    stock_cache: dict[str, pd.DataFrame],
    rs_map: dict[str, int],
    sector_map: dict,
    mktcap_map: dict,
    industry_ranks: dict,
    momentum_candidates: Optional[list[dict]] = None,
    sp500_set: Optional[set[str]] = None,
    nq100_set: Optional[set[str]] = None,
) -> list[dict]:
    """Reverse-funnel scan: find tickers moving on demand TODAY.

    Loops the full stock_cache (all universe tickers with valid history),
    applies the four MOVERS gates, and returns ranked results with
    structure context bolted on so you can distinguish "leader ripping in
    clean base" from "junk pumping on a squeeze."
    """
    if not stock_cache:
        return []

    # Look up which momentum candidates carry BUY ZONE / other signals so we
    # can annotate each mover with "is this in my momentum funnel already?"
    # Uses ticker as the join key. Empty dict is fine — annotation just says No.
    momentum_by_ticker: dict[str, dict] = {}
    if momentum_candidates:
        for c in momentum_candidates:
            t = c.get("Ticker")
            if t:
                momentum_by_ticker[t] = c

    sp500_set = sp500_set or set()
    nq100_set = nq100_set or set()

    rows: list[dict] = []

    for ticker, df in stock_cache.items():
        try:
            if df is None or len(df) < MIN_HISTORY_BARS:
                continue

            close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            if prev_close <= 0:
                continue
            gain_pct = (close - prev_close) / prev_close * 100
            if gain_pct < MIN_GAIN_PCT:
                continue

            today_vol = float(df["Volume"].iloc[-1])
            avg_vol = robust_avg_volume(df["Volume"], window=50, drop_top_pct=0.05)
            if avg_vol is None or avg_vol <= 0:
                continue
            vol_ratio = today_vol / avg_vol
            if vol_ratio < MIN_VOL_RATIO:
                continue

            day_high = float(df["High"].iloc[-1])
            day_low = float(df["Low"].iloc[-1])
            rng = day_high - day_low
            close_pos = ((close - day_low) / rng) if rng > 0 else 0.5
            if close_pos < MIN_CLOSE_POS:
                continue

            rs = int(rs_map.get(ticker, 0))
            if rs < MIN_RS_RANK:
                continue

            if not _trend_template_ok(df):
                continue

            # ---- Passed all gates. Enrich with context. ----
            sector_val, industry_val = sector_map.get(ticker, ("N/A", "N/A"))
            ind_rank_data = industry_ranks.get(industry_val)
            industry_rank = ind_rank_data[0] if ind_rank_data else None

            t_clean = ticker.replace("-", ".")
            if t_clean in sp500_set or ticker in sp500_set:
                index_label = "S&P500"
            elif t_clean in nq100_set or ticker in nq100_set:
                index_label = "NQ100"
            else:
                index_label = "RUT"

            mom_ctx = momentum_by_ticker.get(ticker) or {}
            signals_from_momentum = mom_ctx.get("Signals") or []
            in_buy_zone = "BUY ZONE" in signals_from_momentum
            vcp_quality = mom_ctx.get("VCP_Quality", "N/A")
            pct_above_50ma = mom_ctx.get("Pct_Above_50MA")
            pressure_20d = mom_ctx.get("Pressure_20d")
            days_to_er = mom_ctx.get("Days_To_Earnings")

            # 20-day average DOLLAR volume — same liquidity gauge BUY ZONE uses.
            # Feeds the "Tradeable only ($20M+ ADV)" toggle. Micro-cap movers
            # (DCTH-class) get filtered out by default so the tab isn't a
            # highlight reel of stuff you can't actually trade at your account
            # size. Reuses cached Momentum row's ADV when available to skip
            # recomputing; otherwise computes fresh from the price cache.
            adv_20d = mom_ctx.get("Avg_Dollar_Volume_20d")
            if adv_20d is None:
                try:
                    if len(df) >= 20:
                        _last20 = df.tail(20)
                        adv_20d = float((_last20["Close"] * _last20["Volume"]).mean())
                    else:
                        adv_20d = 0.0
                except Exception:
                    adv_20d = 0.0
            adv_20d = float(adv_20d or 0.0)
            is_tradeable = adv_20d >= TRADEABLE_DOLLAR_ADV

            # 20-day ATR / close as a percentage — daily-range proxy used
            # to normalize gain_pct so mid-caps aren't crushed by micro-cap
            # squeezes in the ranking. See _quality_score docstring.
            try:
                atr_val = calc_atr(df, period=20)
                atr_pct = (float(atr_val) / close * 100.0) if (atr_val and close > 0) else 0.0
            except Exception:
                atr_pct = 0.0

            rows.append({
                "Ticker": ticker,
                "Index": index_label,
                "Sector": sector_val,
                "Industry": industry_val,
                "Industry_Rank": industry_rank,
                "Price": round(close, 2),
                "Gain_Pct": round(gain_pct, 2),
                "Vol_Ratio": round(vol_ratio, 2),
                "Vol_Today": int(today_vol),
                "Avg_Vol_50": int(avg_vol),
                "Close_Pos": round(close_pos, 2),
                "RS": rs,
                "VCP_Quality": vcp_quality,
                "In_Buy_Zone": in_buy_zone,
                "Pct_Above_50MA": pct_above_50ma,
                "Pressure_20d": pressure_20d,
                "Days_To_Earnings": days_to_er,
                "Market_Cap": mktcap_map.get(ticker, "N/A"),
                # Liquidity fields — feed the tradeable toggle
                "Avg_Dollar_Volume_20d": round(adv_20d, 0),
                "Is_Tradeable": is_tradeable,
                # Volatility fields — feed the Quality Score
                "Atr_Pct": round(atr_pct, 2),
                # Two ranking scores side by side. Users pick which lens.
                #   Move_Score    = gain × vol  (raw excitement)
                #   Quality_Score = (gain/atr) × vol  (normalized for volatility;
                #                   preferred default because it doesn't crown
                #                   micro-cap squeezes)
                "Move_Score":    _score_mover(gain_pct, vol_ratio),
                "Quality_Score": _quality_score(gain_pct, vol_ratio, atr_pct),
                # In-momentum flag — did the strict Momentum funnel also
                # surface this? Useful diagnostic: "of tonight's 40 movers,
                # 12 are in my momentum funnel already; the other 28 are
                # names my structural filter is excluding."
                "In_Momentum_Scan": ticker in momentum_by_ticker,
            })
        except Exception:
            continue

    # Default sort: Quality Score (ATR-normalized) so mid-caps aren't
    # crushed by micro-cap squeezes. UI offers a toggle to re-sort by
    # raw Move_Score if the user wants the pure "biggest move + biggest
    # volume" view.
    rows.sort(key=lambda r: r.get("Quality_Score", 0), reverse=True)
    return rows
