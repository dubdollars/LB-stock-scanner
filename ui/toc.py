"""
Table of Contents — definitions for every column, signal, and warning in the
candidates table. Rendered in a popup dialog from the sidebar.
"""

COLUMN_DOCS = {
    "TICKER": (
        "**Stock symbol.** Identifier used by Yahoo Finance / your broker.\n\n"
        "Click any row in the table to open the Deep Dive view for that stock."
    ),
    "RS": (
        "**Relative Strength rating (1-99).** A weighted blend of the stock's "
        "3-month, 6-month, 9-month, and 12-month price returns, ranked against "
        "every other stock in the scanned universe.\n\n"
        "**Range:** 1-99. Higher = stronger performance vs the universe.\n\n"
        "**Scan thresholds:** Elite mode requires ≥85; Standard mode ≥70. "
        "Minervini and O'Neil both consider 80+ the institutional-leadership zone."
    ),
    "TIER": (
        "**Focus-list classification (A / B / C).** Distills the full candidates "
        "list down to actionable groups based on Minervini's framework.\n\n"
        "**Values:**\n"
        "- **A — Trade Now**: RS ≥ 90, VCP grade A or B, within 5% of pivot, "
        "<20% above 50MA, strong fundamentals (EPS+ or Sale+), top-15 industry rank, "
        "no earnings within 14 days. Typically 5-15 names. Click the **Trade Now (A)** "
        "checkbox to filter to these.\n"
        "- **B — Watch List**: RS ≥ 85, VCP A/B/C, within 10% of pivot, <25% above 50MA, "
        "fundamentals OR RS line at new high, no earnings within 14 days. Typically 30-60 names. "
        "Click the **Watch List (B)** checkbox to filter to these.\n"
        "- **C**: Passed the basic SEPA screen but didn't make A or B — usually "
        "because the stock is extended, far from a pivot, or missing one of the "
        "above criteria. Browseable but not focus-list worthy today."
    ),
    "RSI": (
        "**Relative Strength Index (14-period, Wilder's smoothing).** Momentum "
        "oscillator measuring the speed and magnitude of recent price changes.\n\n"
        "**Range:** 0-100. >70 = overbought, <30 = oversold, 50-65 = healthy uptrend."
    ),
    "PEG": (
        "**Price/Earnings to Growth ratio.** Forward PE divided by expected EPS "
        "growth rate. The growth-adjusted version of the P/E ratio.\n\n"
        "**Values:**\n"
        "- Under 1.0 = growth is undervalued relative to its earnings (cheap)\n"
        "- 1.0-2.0 = fairly valued\n"
        "- Over 2.0 = expensive — paying a premium for the growth\n\n"
        "**Note:** Often blank for stocks without analyst growth estimates "
        "(common for very small caps and recent IPOs). From yfinance .info."
    ),
    "🟢 SIGNALS": (
        "**Positive setup tags.** Each pill represents a SEPA leadership "
        "characteristic this stock displays.\n\n"
        "Multiple signals stacked = stronger conviction setup. See the SIGNALS "
        "section below for what each tag means."
    ),
    "🔴 WARNINGS": (
        "**Caution tags.** Each pill represents a risk factor for entering a "
        "new position right now.\n\n"
        "Warnings don't disqualify the stock from the scan — they're flags so "
        "you don't enter blindly. See the WARNINGS section below for each tag."
    ),
    "RS_NH": (
        "**RS Line at New 50-day High?** YES = the stock's relative-strength "
        "line vs SPY just hit a new 50-day high.\n\n"
        "**Values:** YES / —\n\n"
        "Minervini emphasizes this as one of the strongest pre-breakout signals: "
        "the stock is outperforming the market even before the price breakout."
    ),
    "VCP": (
        "**Volatility Contraction Pattern grade.** Classification of the recent "
        "base structure based on actual swing-detected contractions.\n\n"
        "**Values:**\n"
        "- **A** — 3+ contractions, monotonically decreasing depth, current pullback <10%. Best.\n"
        "- **B** — 2+ contractions, current pullback <15%. Good.\n"
        "- **C** — 1+ contraction detected. Marginal.\n"
        "- **None** — No meaningful (≥4%) contraction in the last 80 bars."
    ),
    "VDU": (
        "**Volume Dry-Up.** The most recent COMPLETED trading session's volume "
        "relative to the 50-day average.\n\n"
        "**Important — what 'most recent session' means:** The scanner always strips "
        "any partial intraday bar before computing volume. So if you run the scan "
        "during market hours (or pre-market), VDU reads *yesterday's* full-session "
        "volume — never an in-progress today bar. After-hours runs also use yesterday "
        "(today's bar is stripped because it's hard to tell complete from partial). "
        "So VDU is meaningful any time of day.\n\n"
        "**Values:**\n"
        "- **Extreme** — Volume < 50% of 50-day average (very quiet)\n"
        "- **Yes** — Volume < 50-day average\n"
        "- **No** — Volume > 50-day average\n\n"
        "VDU is great during the basing phase (institutions hold, no selling pressure). "
        "On a breakout day, you actually WANT volume to be high — so 'No' is fine when paired with BO TODAY."
    ),
    "FUND": (
        "**Fundamental tags.** Quarterly EPS and revenue growth flags from the "
        "most recent reported quarter vs the same quarter a year prior.\n\n"
        "**Values:**\n"
        "- **EPS++** — Net income up >25% YoY AND accelerating from the prior quarter\n"
        "- **EPS+** — Net income up >25% YoY\n"
        "- **Sale+** — Revenue up >20% YoY\n"
        "- **Lag** — No qualifying growth (technical setup only)\n"
        "- **N/A** — Yahoo Finance didn't return quarterly financials"
    ),
    "PRICE": "**Today's closing price (USD).** The last completed bar; intraday is excluded.",
    "BUY >": (
        "**Buy stop trigger.** A buy-stop limit order should be placed at this price.\n\n"
        "= Pivot point × 1.005 (a 0.5% buffer above resistance to avoid false breakouts)."
    ),
    "STOP <": (
        "**Hard stop loss.** Computed as the tightest valid choice from:\n\n"
        "- A: Pivot − 1 ATR (volatility-respecting)\n"
        "- B: Most recent swing low in the base\n"
        "- C: Pivot × 0.93 (Minervini's hard 7% ceiling)\n"
        "- D: 50-day MA (if price > 50MA)\n\n"
        "We pick the highest valid stop = tightest acceptable risk."
    ),
    "RISK %": (
        "**Distance from buy trigger to stop, as % of buy price.** Lower = tighter, more attractive.\n\n"
        "**Heuristic:** ≤3% excellent · 3-5% good · 5-7% acceptable · 7-10% wide (WIDE warning) · >10% very wide."
    ),
    "% > 50MA": (
        "**Distance above the 50-day moving average.** Negative would mean below.\n\n"
        "**Range:** typically -10% to +50%. >20% triggers the EXT warning — Minervini avoids initiating positions when extended."
    ),
    "% > 200MA": (
        "**Distance above the 200-day moving average.** A short-side context metric — how stretched the stock is vs its long-term trend.\n\n"
        "Most stocks worth shorting on a short-term overbought setup will still be **above** the 200-day MA "
        "(otherwise they\'re already in a downtrend, and the easy money has been made). Healthy mean-reversion "
        "shorts typically read 5-40% above. Negative readings often mean the stock has already broken down."
    ),
    "VOL ×": (
        "**Today's volume as a multiple of the 50-day average.** 1.0x = normal volume; 2.0x = double; etc.\n\n"
        "On a breakout day, ≥1.5x is institutional confirmation. <1.0x = no buying interest."
    ),
    "ER (d)": (
        "**Days until next earnings report.** Negative if earnings already past.\n\n"
        "**Range:** typically -90 to +90. Within 7 days fires the ER warning — don't initiate swing positions immediately before earnings.\n\n"
        "**N/A** = Yahoo Finance didn't have an earnings calendar entry."
    ),
    "ER DATE": (
        "**Date of the next scheduled earnings release** (YYYY-MM-DD).\n\n"
        "From Yahoo Finance's earnings calendar. Sometimes missing for small-cap names."
    ),
    "INDEX": (
        "**Which major US index this ticker belongs to.**\n\n"
        "**Values:** S&P500 · NQ100 · RUT (Russell 2000 / 3000)"
    ),
    "IND #": (
        "**Industry group rank by mean RS.** 1 = strongest industry group in the scanned universe.\n\n"
        "**Range:** 1 to ~100 (depends on how many industries have ≥3 constituents). "
        "Top-15 industries are typically where Minervini wants to fish."
    ),
    "MKT CAP": (
        "**Market capitalization** (current price × shares outstanding).\n\n"
        "**Format:** $X.XT (trillion), $X.XB (billion), $XM (million)."
    ),
    "SECTOR": (
        "**Yahoo Finance sector classification** (high-level grouping).\n\n"
        "Examples: Tech, Health, Finance, Cons.Cyc, Cons.Def, Comm.Svc, Materials, RE, Indust., Energy, Utilities."
    ),
    "INDUSTRY": (
        "**Yahoo Finance industry classification** (sub-sector). "
        "More granular than sector — e.g., 'Semiconductors' inside the Tech sector. "
        "Industry group leadership is one of Minervini's key filters."
    ),

    # ---- Short Candidates page columns ----
    "SETUP (short)": (
        "**Short setup tier** (Short Candidates page).\n\n"
        "**Values:**\n"
        "- **CONFIRMED** — At least one actionable today-bar weakness trigger fired: "
        "distribution day (high-vol down day), bearish reversal candle (shooting star, "
        "engulfing, hanging man), Bollinger Band upper rejection, OR closed below "
        "yesterday's low after recent new highs. Higher-probability entries.\n"
        "- **ANTICIPATE** — Stock is overbought + stretched but hasn't shown actual "
        "weakness on today's bar yet. Riskier — you're betting on imminent reversal "
        "without confirmation.\n\n"
        "Note: weekly RSI ≥ 70 is shown in the SIGNALS column and boosts the score, "
        "but doesn't auto-promote to CONFIRMED. It's a *condition* (multi-timeframe "
        "overbought), not a *trigger* (something bearish happened today). Without that "
        "distinction, broadly overbought markets would put nearly every candidate into "
        "CONFIRMED and leave ANTICIPATE empty."
    ),
    "SCORE (short)": (
        "**Short composite score (0-100).** Higher = higher-probability mean-reversion setup.\n\n"
        "Built from: RSI level (75-85+), % above 50MA stretch, CONFIRMED tier bonus, "
        "Connors RSI(2) extreme, climax run, BB upper rejection, distribution day, "
        "bearish candle, weekly overbought, ATR stretch, and a market-overbought bonus when applicable.\n\n"
        "**Penalties:** earnings within 14 days (binary catalyst risk), top-3 industry leader "
        "(true leaders pull back less reliably)."
    ),
    "RSI(2)": (
        "**Connors 2-period RSI.** Much faster/more sensitive than the standard RSI(14).\n\n"
        "**Range:** 0-100. >95 = extreme short-term overbought (Connors's classic mean-reversion entry); "
        "<5 = extreme oversold."
    ),
    "WK RSI": (
        "**Weekly RSI (14-period).** Computed by resampling daily closes to weekly bars.\n\n"
        "**Range:** 0-100. >70 on the WEEKLY chart means overbought across multiple time frames — "
        "much stronger reversal signal than just daily RSI > 70 in isolation."
    ),
    "ATRx": (
        "**ATR Stretch.** Distance of current price above the 21-day EMA, measured in ATR(20) units.\n\n"
        "**Range:** typically -3 to +5. Volatility-normalized version of '% above MA' — accounts for "
        "whether the stock is normally calm or wild.\n\n"
        "- **>2.5 ATRs** = stretched\n"
        "- **>3.5 ATRs** = extreme (high mean-reversion probability)"
    ),
    "RUN 15d": (
        "**% gain over the last 15 trading bars (~3 weeks).**\n\n"
        "Minervini's climax-top criterion: 25%+ gain in 15 bars on accelerating volume = "
        "exhaustion in motion. Triggers the **CLIMAX RUN** signal pill when both conditions hold."
    ),
    "SHORT <": (
        "**Sell-stop short entry trigger.** = today's low * 0.995 (0.5% below today's low).\n\n"
        "Place a sell-stop order at this price. When the stock breaks below today's low, your short fills. "
        "Waiting for the break confirms weakness — you are not picking the top, you are shorting after price action confirms reversal."
    ),
    "COVER >": (
        "**Buy-to-cover stop.** = max(today high, 5-day swing high) * 1.01 (1% above the high).\n\n"
        "If price closes above this level, the short is invalidated — cover and move on."
    ),
    "-> 20MA": (
        "**First profit target: 20-day moving average.** Quick mean-reversion target, typically reached in 5-10 trading days.\n\n"
        "Conservative target. Take partial profits here."
    ),
    "-> 50MA": (
        "**Second profit target: 50-day moving average.** Deeper pullback target, typically reached in 1-3 weeks.\n\n"
        "Where most overbought stocks find genuine support."
    ),
}


SIGNAL_DOCS = {
    "BO TODAY": (
        "**Breakout (most recent completed session).** The stock closed above its "
        "pivot point AND volume on that session is >= 1.5x the 50-day average. "
        "Like VDU, this uses the most recent FULL trading day - partial intraday "
        "bars are always stripped, so 'TODAY' here means the latest complete session "
        "regardless of when you actually run the scan. This is the SEPA institutional-"
        "confirmation signal - you want the breakout to happen on heavy volume, not "
        "anemic volume."
    ),
    "POWER PLAY": (
        "**Minervini Power Play setup.** Stock has advanced 100%+ in the last 8 weeks "
        "AND has been consolidating within ~25% of the recent peak for at least 3 weeks. "
        "These are some of Minervini's highest-probability setups."
    ),
    "RS check": (
        "**RS line at new 50-day high.** The stock's relative-strength line vs SPY just "
        "hit a new 50-day high - confirms institutional leadership even if price hasn't "
        "fully broken out yet."
    ),
    "VCP-A": (
        "**VCP grade A.** 3+ contractions with monotonically decreasing depth, current "
        "pullback <10%. The textbook Minervini volatility contraction pattern."
    ),
    "VCP-B": (
        "**VCP grade B.** 2+ contractions, current pullback <15%. Cleaner-than-average "
        "base structure, just not the perfect 3-stage tightening of grade A."
    ),
    "BZ-LITE": (
        "**BUY ZONE variant without the VCP-grade requirement** (added 2026-08-04). "
        "Same pressure, near-50MA, and $20M ADV gates as full BUY ZONE — but "
        "drops the requirement for a formal VCP pattern (A/B/C grade). No "
        "substitute quality gate: because volatility contraction is essentially "
        "what VCP measures, adding a vol-ratio filter here just recreated the "
        "original requirement and produced zero candidates. This version leans "
        "on the pressure + near-50MA + ADV gates alone as evidence of "
        "institutional accumulation. **Signal labels are deduplicated** — a "
        "stock is tagged BUY ZONE OR BZ-LITE, never both — but **the filter "
        "surfaces the SUPERSET**. Clicking BZ-LITE shows every BUY ZONE stock "
        "PLUS every stock that would have qualified except for missing VCP. "
        "That matches the 'widen the funnel' intent. The underlying trend "
        "template (price above 50MA > 150MA > 200MA, 200MA rising, RS ≥ 70/85) "
        "is still enforced. Expect more false positives than full BUY ZONE — "
        "sift the charts manually. Best used when BUY ZONE narrows and you "
        "need a wider hunt zone."
    ),
    "PEAD": (
        "**Post-Earnings Announcement Drift** (added 2026-08-04). Institutions "
        "accumulate winners in the 3-15 sessions after an earnings beat. All five "
        "gates must fire: (1) earnings happened 3-15 trading days ago, (2) that "
        "session gapped up 5%+ on 2x+ volume AND closed in the upper half of the "
        "day's range, (3) since the gap, no session has closed below the earnings-"
        "day open (base held), (4) recent 5-day volume is BELOW the 50-day average "
        "(quiet accumulation, not distribution), (5) today's close is within 5% of "
        "the post-earnings high (consolidating near the top, not rolling over). "
        "Fundamentally-driven — works when VCP fails because the catalyst provides "
        "its own institutional demand. Especially valuable in high-vol tapes where "
        "traditional consolidation patterns break down."
    ),
    "RETEST": (
        "**Breakout retest / second entry** (added 2026-08-04). Ticker broke out "
        "3-15 sessions ago (from the persistent breakout log), has held above the "
        "original pivot every session since (no fill), and today's close is within "
        "3% of that pivot on quiet volume (last 5 sessions ≤ 50-day avg). "
        "Minervini's 'add on strength' setup — you're buying a PROVEN breakout at a "
        "lower-risk price rather than chasing the initial extension. Highest hit "
        "rate of the newer additions because you already have the volume "
        "confirmation from the original breakout day. Requires the persistent "
        "breakout log — will fire more often as your scan history builds up."
    ),
}


WARNING_DOCS = {
    "EXT": (
        "**Extended.** Price is more than 20% above the 50-day MA. Minervini "
        "specifically avoids initiating new positions in extended stocks because "
        "they're due for mean-reversion. This is the most common reason a leader "
        "appears in the scan but isn't a buyable setup right now."
    ),
    "ER Nd": (
        "**Earnings within N days.** Earnings reports are binary catalysts that "
        "can erase a setup overnight. Initiating a swing position right before "
        "earnings is gambling, not trading. Wait until after the report."
    ),
    "WIDE": (
        "**Wide stop.** Risk per share exceeds 7% of buy price. Either the stop "
        "is too far below the pivot (pattern is loose), or the stock is too "
        "volatile relative to its current setup. Reduces the R-multiple math."
    ),
    "SQUAT": (
        "**Bearish reversal candle (most recent session).** The most recent bar "
        "made a new high above the prior 10-day range BUT closed in the bottom "
        "half of that day's range. Classic 'squat' = institutional distribution "
        "at the highs. Avoid this setup."
    ),
}


# ---------------------------------------------------------------------------
# Warnings appropriate for the SHORT trader. These are reasons NOT to initiate
# a short (or to be cautious about doing so) even when the green-signal stack
# looks tempting. Some overlap with the long warnings (earnings risk, wide
# stops apply to either side) but the rest are short-specific.
# ---------------------------------------------------------------------------
WARNING_DOCS_SHORT = {
    "ER Nd": (
        "**Earnings within N days.** Same risk as longs in reverse — a beat-and-raise "
        "after-hours could rip the stock 10-20% higher overnight, well past your "
        "cover stop. Short squeezes on earnings are how short books blow up. Wait "
        "until after the report."
    ),
    "WIDE": (
        "**Wide cover stop.** Risk per share exceeds 7% of entry price. Either the "
        "cover stop is too far above your short trigger (pattern is loose) or the "
        "stock is too volatile to size meaningfully. Reduces the R-multiple math."
    ),
    "STRONG TREND": (
        "**Stock is in a confirmed uptrend with strong fundamentals.** RS line "
        "at new high, big positive EPS+/Sale+, leadership group. Shorting genuine "
        "leaders against the primary trend is a low-probability trade — even if "
        "the short-term setup looks exhausted, real leaders often consolidate "
        "sideways instead of correcting. Stick to weaker names for mean-reversion "
        "shorts."
    ),
    "LIGHT VOLUME": (
        "**Climax run on declining volume.** A real exhaustion top needs accelerating "
        "volume on the run-up — without it, the move may just be a quiet drift higher "
        "with no institutional distribution to fade. Shorting drift = catching a "
        "falling knife if no real buyers are exhausted."
    ),
    "MARKET UP/DN VOL > 1": (
        "**Broad market is in accumulation.** When U/D volume is meaningfully above 1.0 "
        "across the universe, individual exhaustion shorts have a much lower hit rate — "
        "you\'re fighting a tide of net buying. Best short setups fire when broad "
        "internals are deteriorating, not improving."
    ),
}


# ---------------------------------------------------------------------------
# Long-side signals are stored above in SIGNAL_DOCS. We freeze them under a
# separate name so callers that want ONLY long-side signals (e.g. the long
# Table of Contents) can grab the long set without short-side bleed-through.
# ---------------------------------------------------------------------------
SIGNAL_DOCS_LONG = dict(SIGNAL_DOCS)


SIGNAL_DOCS_SHORT = {
    "CLIMAX RUN": (
        "**Climax run.** Stock is up 25%+ over the last 15 trading bars on accelerating volume "
        "(last 5-bar avg volume > prior 5-bar avg by 15%+). Minervini's exhaustion-top signal — "
        "parabolic moves on rising volume usually mark local tops."
    ),
    "DIST DAY": (
        "**Distribution day.** Today closed lower than yesterday on volume > 1.5x the 50-day average, "
        "AND price was within 7% of the 20-day high before today. Translation: institutions started "
        "selling at the highs. Classic exhaustion signal."
    ),
    "BEARISH CANDLE": (
        "**Bearish reversal candle today.** One of: Shooting Star, Bearish Engulfing, or Hanging Man "
        "fired on the most recent bar after recent new highs."
    ),
    "BB UPPER REJ": (
        "**Bollinger Band upper rejection.** Today's high touched or exceeded the upper Bollinger Band "
        "(20-period, 2 sigma), but the close fell back inside. Classic mean-reversion entry signal."
    ),
    "BROKE PRIOR LOW": (
        "**Broke prior day's low after recent new highs.** First sign of weakness — stock made new highs "
        "in the last 5 days, then today closed below yesterday's low."
    ),
    "WEEKLY OB": (
        "**Weekly RSI >= 70.** Multi-timeframe overbought confirmation. Daily RSI > 70 in isolation often "
        "fails in strong uptrends; weekly RSI also overbought materially raises the mean-reversion probability."
    ),
    "RSI2>=95": (
        "**Connors RSI(2) >= 95.** Extreme short-term overbought reading using Connors's 2-period RSI. "
        "Statistically the highest-win-rate single-signal mean-reversion entry in academic studies."
    ),
}


# Backward-compat alias kept for older callers that still import SIGNAL_DOCS
# expecting the merged dict. New code should import SIGNAL_DOCS_LONG or
# SIGNAL_DOCS_SHORT explicitly. We keep SIGNAL_DOCS as the FULL merged dict
# (long + short) only because some short-side rendering code reads from it.
SIGNAL_DOCS_SHORT_EXTRA = SIGNAL_DOCS_SHORT  # legacy alias
SIGNAL_DOCS = {**SIGNAL_DOCS_LONG, **SIGNAL_DOCS_SHORT}
