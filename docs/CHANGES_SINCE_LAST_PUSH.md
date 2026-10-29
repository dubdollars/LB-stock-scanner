# Legacy Brands Money Making Machine — What's New

*Every change since your last pull (commit `0e22b8d`, June 28) — includes both today's push and this session's work.*

---

## 🎯 Dashboard / Market Signal — Biggest Overhaul

**Distribution-day math rewritten (critical).** The old code compared each day's volume to the 50-day average, which produced false positives around expiration/quarter-end and false negatives on real distribution days (like July 13 and July 17). The count could even change *retroactively* between scans as the sliding average moved. **Now uses the strict IBD rule**: index down ≥0.2% AND today's volume > yesterday's volume. Frozen historical facts, deterministic, no more mystery count changes.

**Data source swapped from ^GSPC / ^IXIC / ^RUT to SPY / QQQ / IWM.** Yahoo's index-level volume feeds are patchy — the ETF proxies have clean, consistent share volume. Prices move ~1:1 with the indexes so the price test is unaffected; the count is just more reliable now.

**Follow-Through Day detection + market state machine (major new capability).** A new info box under the yellow signal banner shows where the market is in the O'Neil cycle: 🟢 **UPTREND** / 🔴 **CORRECTION** / 🟡 **RALLY ATTEMPT — Day N**. During a rally attempt, the panel tells you exactly what it would take for tomorrow to print an FTD (any of SPY/QQQ/IWM up 1.25%+ on higher volume, from Day 4 onward). Once an FTD prints, state flips to UPTREND automatically. The "🔬 Market state detail" expander shows correction low, day-of-rally-attempt, and every FTD in the window. Strong FTDs (≥1.7%) marked 🔥. Threshold set to 1.25% instead of IBD's 1.4% — catches borderline confirmations that a stricter reading misses.

**Distribution date list.** Hover the "Dist days: N" number in the yellow banner to see the actual dates counted. Click the 📅 **Distribution day dates** expander for a full formatted list per index with explainer.

**Persistent scan log.** Every healthy scan appends to `data/scans/market_state_log.jsonl` — distribution dates + state snapshot. Lets you diff scan-to-scan when something changes and keeps an auditable trail.

---

## 📓 Journal → renamed **Performance**

**Tab renamed** — button now reads "📓 PERFORMANCE", page heading is "Performance Dashboard".

**Auto-refresh prices on tab entry.** Click 📓 PERFORMANCE and yfinance runs automatically for your held tickers. Manual Refresh Prices button still available for re-refreshes.

**Edit Notes button.** ✏️ Edit Notes below the notes panel on any trade — full text-area edit with Save / Cancel. Reads ✏️ Add Notes on trades that have none.

**New dashboard tiles** (10 total, in 2 rows of 5):
- Row 1: OPEN POSITIONS, CLOSED, WIN RATE, EXPECTANCY (R), LIVE EXP (R)
- Row 2: REALIZED P&L, OPEN P&L, TOTAL P&L, VALUE, LIVE RISK
- Hover any tile for the definition

**New position-tracking capabilities:**
- **Move Stop button** with immutable initial-stop anchor (so trailing stops up doesn't retroactively inflate your R-multiples)
- **Timestamped action notes** appended to trade Notes automatically when you Add / Sell / Move Stop
- **Decision Snapshot panel** — per-row diagnostic showing 4 sections of setup quality
- **Source field** — track which scan the trade came from (separate from Setup tag)
- **Days Held** column
- **Open R-Multiple** column (live position tracking)
- **Cost $ / Last $ / Last Date / P&L $ / G/L % / Risk % / Live Risk** columns
- **Open / Closed / All** filter buttons above the table
- **Excel export** works from position-level table

**UI cleanup this session:** removed the little ⓘ tile icons (kept hover tooltips), removed the Value column, removed the redundant HELD POSITIONS totals strip below the table.

---

## 🔄 Rotation — Aligned to Minervini's Actual Methodology

**Data source improved.** Rotation now sources from Quality scans instead of Momentum scans — broader universe (~3,000 stocks vs. ~150) and more history available. Fixed the "empty 1w/4w columns" and "partial-scan picker" bugs that plagued the old version.

**Hang bug fixed** — Rotation tab now loads instantly instead of hanging 30-60s.

**Four new Minervini-style columns:**
- **LDR %** (Leadership Breadth): % of members with RS ≥ 80 — filters out "one mega-cap carrying the mean"
- **NH %** (New-Highs Breadth): % of members within 5% of 52-week high — coordination signal
- **SETUP %** (Setup Density): % of members currently in Momentum scan with valid setup
- **THE GENERAL / GEN RS / GEN SIG**: highest-RS "canary" stock in the group with its current signal

**Two filter buttons:**
- **🌱 BUDDING LEADERS**: Δ4w ≥ +15 AND rank 15-50 (early-but-confirmed rotation)
- **🚀 EMERGING** (new): Δ4w ≥ +15 AND rank ≤ 30 (climbing into leadership)

**Full drill-down** on any industry row — see every member with Composite, EPS, RS, U/D Vol, fundamentals. Contextual "← Back to [Industry]" navigation from stock deep-dive.

**Rotation TOC rewritten** — organized into Core Ranking / Minervini Leadership Signals / Filter Buttons / Drill-Down sections.

---

## 📈 Momentum Tab

**BUY ZONE signal + filter** — Triple Barrel setup (Pressure + near 50MA + Tight Price Range). $20M average dollar-volume liquidity gate. Cohort Tracker dialog that logs BUY ZONE candidates and tracks their pivot-clear-first outcomes with proper Minervini semantics.

**20-day Pressure column** — momentum-vs.-supply metric.

**Signals overhauled** — Power Play tightened, new CHEAT signal, new GAP UP signal, full TOC signal documentation.

**VCP A Only + VCP B Only** filter buttons.

---

## ⭐ Top Stocks Tab

**Industry-rank tile/table sync fixed** — ELITE PICKS count now correctly includes the industry-rank filter.

---

## 🐛 Bug fixes & Infrastructure

- **Rate-limited scan guardrail** — scanner refuses to overwrite `latest.json` when the universe collapses below 1,500 tickers (yfinance rate-limit protection)
- **Sleep prevention** on all long-running scans (Momentum, Quality, Mean Reversion, Social)
- **`_prune_old` bug fixed** — was accidentally wiping historical Momentum scans
- **Market cap formatting** unified with M/B/T suffixes across all tables
- **Persistent BUY ZONE cohort log** at `data/buy_zone_cohort.jsonl`
- Atomic writes for scan files (prevents corruption on interrupted saves)

---

## 💡 What This Means for Your Workflow

**Before entering trades**, check the yellow banner AND the state pill below it. If state shows CORRECTION or a young RALLY ATTEMPT (Day 1-3), the O'Neil framework says wait for FTD confirmation before pressing new positions.

**On the Rotation tab**, the four new columns turn it from a "which sectors are up" screen into a Minervini-style leadership finder. High LDR% + high NH% + a buyable General = the real thing.

**On the Performance tab**, no more clicking Refresh Prices — you get fresh P&L the moment you open the tab. And now you can edit notes after the fact for cleaner trade records.

**On the Momentum tab**, BUY ZONE is the setup that best captures Minervini's "pressure at the buy point" concept. Combine with the Cohort Tracker to see how the framework is performing over time.
