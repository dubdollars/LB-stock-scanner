# Changes since last push — Batch 3

A summary of what's new and why. Grouped by area of impact so you can find the pieces most relevant to your workflow.

---

## New signals — wider funnel for finding candidates

### PEAD (Post-Earnings Announcement Drift)
Fires when a stock had a strong catalyst-day gap in the last 3–15 sessions (5%+ gap on 2x+ volume, closed strong), then held the gap and is now consolidating quietly near the post-catalyst high. Purely price-based — no dependence on earnings-date metadata (yfinance's date is unreliable). Catches AI names / earnings winners that are drifting on continued institutional accumulation. New filter button on Momentum.

**Why:** BUY ZONE + VCP wasn't catching stocks that ran on real news and are now consolidating. PEAD fills that gap. Especially valuable post-earnings season.

### RETEST (Breakout Retest / Second Entry)
Fires when a ticker that fired BO TODAY 3–15 sessions ago is now pulling back within 3% of its original pivot on quiet volume, and has held above the pivot the whole time. Minervini's "add on strength" second-entry setup — proven breakout, lower-risk price. Highest hit rate of the new signals.

**Why:** First entries at the breakout often get hit intraday and the good ones come back. RETEST catches the second chance. Requires a persistent breakout log (new file: `data/breakout_log.jsonl`) that builds up over time — the signal will be quiet for the first ~3 days as the log fills.

### BZ-LITE (BUY ZONE — wider funnel variant)
Same pressure, near-50MA, and $20M ADV gates as BUY ZONE, but drops the VCP-grade requirement. Signal labels are deduplicated (a stock is either BUY ZONE or BZ-LITE, never both) but the filter shows the **superset** — clicking BZ-LITE surfaces every BUY ZONE stock PLUS every stock that would qualify except it lacks a formal VCP pattern.

**Why:** In high-volatility regimes, clean VCP structure can't form even on genuine leaders. BUY ZONE narrows to a handful precisely when you'd want a wider funnel. BZ-LITE opens up the hunt zone without abandoning the institutional-accumulation gates.

---

## Adaptive BUY ZONE

BUY ZONE gates now loosen automatically based on market state:

- **Fresh UPTREND** (within 5 sessions of an FTD): pct_50ma cap 10% → 15%, pressure floor 1.3 → 1.15
- **RALLY_ATTEMPT Day 4+** (FTD imminent): pct_50ma cap 10% → 12%, pressure floor 1.3 → 1.20
- **Otherwise**: standard gates

Snaps back to normal once environment normalizes or if the FTD fails. The market state detail expander now shows a **BUY ZONE regime** line (TIGHT / LOOSE fresh FTD / LOOSE FTD watch) so you can see which gate profile was applied.

**Why:** On FTD day, the big index rally pushes candidates past pct_50ma caps and skews pressure ratios. That throttled the BUY ZONE list precisely when we most wanted candidates.

---

## FTD failure detector — fresh-dist counter (NEW Control Tower intelligence)

Two distribution-day counters now run in parallel with different jobs:

1. **Trailing-25 counter** (existing) — general environment gauge. Powers the RED/YELLOW/GREEN market signal banner. Doesn't reset when we get an FTD.
2. **Fresh post-FTD counter** (NEW) — only active in UPTREND. Counts fresh distribution days that occur AFTER the FTD, resets to 0 at every new FTD. If 2+ fresh dist days accumulate within 10 trading sessions post-FTD, the state machine automatically downgrades UPTREND → CORRECTION.

The market state detail expander now displays: **Fresh dist post-FTD: N / 2** with color coding + actual fresh dist dates + sessions past FTD. Red when one more dist day away from downgrade.

**Why:** The IBD/O'Neil rule for FTD failure is "2–3 fresh dist days within 2 weeks kills the FTD." A third to half of FTDs die exactly this way. The trailing-25 counter alone can't tell you this because it doesn't know when the FTD happened.

---

## BREAKOUT signal — tightened to eliminate false positives

The BO TODAY signal now requires FOUR gates instead of two:

1. Close > pivot (existing)
2. Volume ≥ 1.5x 50d avg (existing)
3. **Close > prior close** (NEW — up on the day)
4. **Close in upper half of today's range** (NEW — strong close)

**Why:** The old two-gate logic fired BO TODAY on FTRE on a -5% down day because the close still happened to sit above a stale earlier-base pivot on elevated volume. A breakout should be a bullish ACTION, not a coincidence. Big red bars where volume is elevated (i.e., distribution) no longer trigger.

---

## Cohort Tracker fixes

### Strict milestone logic
+5% and +10% reached dates now require: (1) pivot must clear first (otherwise no trade was ever entered) AND (2) stop must not have been hit before the milestone. This makes the tiles measure "would a realizable trade have worked" instead of raw cohort momentum.

**Why:** Prior version counted any peek above +N% at any moment, even if the trade never triggered or was stopped out first. Not honest.

### Tile math bug fix
The +5% and +10% tile percentages now use the same strict rule as the drill-down tables. Before, tile % was based on max-return-since-flag (lenient) while table showed only strict wins. Numbers matched at the count level but not the percentage level — confusing side-by-side.

### WINS and LOSSES drill-down tables
Two new expanders below the +5%/+10% tables:
- **🏆 WINS** — every ticker that hit +2R before -1R (the payoff studies)
- **💥 LOSSES** — every ticker that hit -1R before +2R (the failure studies)

Sorted by Max % descending so the biggest movers surface first. Both anchored to actual pivot/stop rules — no partial credit.

**Why:** Studying failures teaches as much as studying wins. Now the +2R hit-rate tile has its own drill-down like the +5% and +10% tiles.

---

## UI / quality-of-life changes

### Momentum is now the default tab
Opening the scanner lands on Momentum instead of Top Stocks. Rationale: Momentum has the fullest signal breakdown (BUY ZONE, BZ-LITE, RETEST, PEAD, VCP, etc.), while Top Stocks is the intersection view — narrower and less useful as an entry point.

### Filter buttons reordered by signal priority
The Momentum filter row is now:

**BUY ZONE → BZ-LITE → RETEST → PEAD → BREAKOUT → VCP A → VCP B → TRADE NOW → WATCH LIST → COHORT**

Signal filters first (in entry-signal priority order), tier filters last, COHORT dialog at the end. Makes the workflow flow left-to-right in decreasing quality / increasing net-width.

### Eastern Time display for all timestamps
The LAST SCAN box and "Scan timestamp:" line now show ET (EST/EDT) regardless of where the scanner runs (local machine or Streamlit Cloud). Uses stdlib zoneinfo. If you see a timestamp ending in `?`, that means the tz database is missing — install `pip install tzdata` (rare).

**Why:** On Streamlit Cloud, host is UTC and timestamps displayed 4-5 hours off from your trading day.

---

## Control Tower dialog updates

Stage 6 (Confirmation window post-FTD) rewritten to:
- Reference the automated fresh-dist counter (no more manual counting)
- Explain the two-counter architecture (trailing-25 vs fresh-post-FTD) so it's clear which counter does what
- Update the "when to close probes" rule to key off the fresh counter hitting 2

---

## TOC (Table of Contents) additions

New signal docs added for PEAD, RETEST, and BZ-LITE — full explanations of criteria, when they fire, why they exist, and how to interpret them. Accessible from the "TOC" button on the Momentum tab.

---

## Files changed / added

**New file:**
- `scanner/breakout_log.py` — persistent breakout log module (append-only jsonl at `data/breakout_log.jsonl`)

**Modified files:**
- `app.py` — Momentum default, filter reorder + new buttons, ET timezone, market state expander additions, Control Tower Stage 6 rewrite
- `scanner/core.py` — fresh-dist counter, adaptive BUY ZONE, BZ-LITE, PEAD wiring, RETEST wiring, buy_zone_regime diagnostic
- `scanner/setups.py` — new detect_pead + detect_breakout_retest, BREAKOUT tightened to 4 gates
- `scanner/cache.py` — append_breakout_snapshot hook after each scan save
- `ui/cohort_view.py` — strict milestone walk, tile math fix, WINS/LOSSES drill-down tables
- `ui/toc.py` — SIGNAL_DOCS additions for PEAD, RETEST, BZ-LITE

---

## What to try first

1. **Look at the market state detail expander** — new fresh-dist post-FTD counter and BUY ZONE regime lines
2. **Click BZ-LITE filter** — should show a superset of BUY ZONE (all BUY ZONE stocks + non-VCP-qualifying candidates)
3. **Wait 3–4 more scans, then click RETEST** — needs the breakout log to build up first
4. **Open the Cohort Tracker → scroll to WINS / LOSSES** — new drill-downs for +2R decided flags
5. **Open the Control Tower dialog → Stage 6** — updated playbook for the confirmation window after a fresh FTD
