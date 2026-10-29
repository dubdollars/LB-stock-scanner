# Getting Started — Legacy Brands Money Making Machine

A momentum-focused stock scanner that pulls daily data from Yahoo Finance, applies Mark Minervini / O'Neil SEPA filtering rules, surfaces entry signals (BUY ZONE, RETEST, EP, PEAD, BREAKOUT), and tracks live positions through a built-in trade journal with R-multiple accounting.

This guide is for **anyone who wants to run it** on their own computer.

It assumes Windows. If you're on Mac, the steps are nearly identical — substitute the obvious things (e.g. `python3` instead of `python`, forward slashes in paths, Terminal instead of cmd).

---

## What you'll need (~30 minutes the first time)

1. Python 3.11 or newer
2. Git for Windows

You'll only do these once. After that, running the scanner is just double-clicking a `.bat` file.

---

## Step 1 — Install Python

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow "Download Python 3.X.X" button (current latest)
3. Run the installer
4. **IMPORTANT:** on the very first installer screen, check the box at the bottom that says **"Add python.exe to PATH"** before clicking "Install Now". This is critical — without it, your computer won't find Python from the command line.
5. Click "Install Now" and wait
6. When done, open a new Command Prompt (Windows key → type `cmd` → Enter) and verify with:
   ```
   python --version
   ```
   You should see something like `Python 3.12.X`. If you see "not recognized", Python wasn't added to PATH — uninstall and reinstall with the checkbox.

---

## Step 2 — Install Git

1. Go to **https://git-scm.com/download/win**
2. The download starts automatically; run the installer when done
3. The installer has ~11 screens of options. Just click **Next** through all of them — defaults are fine. One small recommendation: on the "Choosing the default editor" screen, pick **"Use Notepad as Git's default editor"** from the dropdown (the default Vim editor is harder to use for non-coders).
4. After install, open a **new** Command Prompt and verify with:
   ```
   git --version
   ```
   You should see `git version 2.X.X.windows.1` or similar.
5. Configure Git with your identity:
   ```
   git config --global user.name "your-github-username"
   git config --global user.email "your-email@example.com"
   ```
   Any name and email you like — a public clone needs no account, this is just how git labels any future commits you make locally.

---

## Step 3 — Clone the repo to your computer

1. Pick a location for the code on your computer. Common choice: `C:\Users\<your-name>\Documents\` or your Desktop.
2. Open Command Prompt and navigate there:
   ```
   cd "C:\Users\<your-name>\Documents"
   ```
   (Use your actual user name and folder.)
3. Clone the repo:
   ```
   git clone https://github.com/dubdollars/LB-stock-scanner.git
   ```
   The repo is public, so no sign-in is required.
4. After cloning, a new folder called `LB-stock-scanner` will appear with all the code. Enter it:
   ```
   cd LB-stock-scanner
   ```

---

## Step 4 — Set up the Python environment

The scanner uses a self-contained Python environment so its packages don't conflict with anything else on your computer. We create that environment now (one-time setup).

1. From inside the `LB-stock-scanner` folder:
   ```
   python -m venv .venv
   ```
   This creates a `.venv` folder. Takes 5-10 seconds. No output expected.

2. Activate the environment:
   ```
   .venv\Scripts\activate
   ```
   You'll see `(.venv)` prepended to your command prompt — that means the environment is active.

3. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```
   This downloads and installs ~30 packages. Takes 2-5 minutes depending on your internet. Output will scroll a lot — that's normal.

---

## Step 5 — Create your own secrets file

The repo deliberately doesn't include the `secrets.toml` file (it contains private values like passwords). You create your own local copy.

1. Inside the `LB-stock-scanner` folder, navigate to `.streamlit\`:
   ```
   cd .streamlit
   ```
2. Copy the example file:
   ```
   copy secrets.toml.example secrets.toml
   ```
3. Open the new `secrets.toml` in Notepad and leave `app_password = ""` (empty) — that disables the password gate so you can use the scanner directly on your own machine without typing a password every time.
4. Save and close.
5. Go back to the project root:
   ```
   cd ..
   ```

---

## Step 6 — Run the scanner

1. From the `LB-stock-scanner` folder, double-click **`run.bat`**.
2. A Command Prompt window will open and start the Streamlit server.
3. Wait ~5-10 seconds — your default browser should auto-open to `http://localhost:8501` with the scanner dashboard.
4. If the browser doesn't open automatically, manually open it and type `localhost:8501` in the address bar.

The dashboard is now running on your machine.

---

## Step 7 — Run your first scan

1. In the sidebar on the left, make sure **ELITE MODE** is toggled on (red toggle, with text "ELITE MODE (RS>=85, EPS REQUIRED)").
2. Click **Run Scan**.
3. Wait. **The first scan takes 30-60 minutes** because it has to download 540 days of price history for ~3,225 stocks from Yahoo Finance. Subsequent scans are faster (10-20 minutes) because data is cached locally and only the last few days are refreshed.
4. While waiting, you can leave the cmd window open in the background — Streamlit runs the scan and updates the dashboard when done.
5. When complete, the **Momentum tab** shows a list of candidates with RS rating, signals, warnings, suggested buy triggers, and stop prices. Click any ticker to drill into the deep-dive page.

**Important:** don't hit Run Scan more than once a day under normal circumstances. Yahoo Finance throttles IPs that scrape too aggressively, and once you're on their throttle list it can take days to come off. See the Troubleshooting section if things go wrong.

---

## Pulling updates later

When the repo owner makes improvements, you can fetch their changes without re-doing all the setup. From the `LB-stock-scanner` folder:

```
git pull
```

That downloads any new code. If new Python packages were added, also run:

```
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## What's in this scanner

The dashboard has seven top-level modes, selected via the button row at the top. From left to right:

**TOP STOCKS** (default home) — intersection view that surfaces only tickers passing BOTH the Momentum filter AND the Quality filter. Fewer names, higher conviction.

**MOMENTUM** — the main SEPA scan. Applies Minervini trend template (price > 50MA > 150MA > 200MA, 200MA rising, RS >= 85 in Elite mode), detects multiple entry signals (BUY ZONE, RETEST, EP, PEAD, BO TODAY, CHEAT, POWER PLAY, GAP UP, RS-check, VCP-A, VCP-B), and shows pivot points, stops, and sizing. Filter row lets you narrow to a single signal type.

**MOVERS** — reverse-funnel scan. Instead of starting from structure and hoping for demand, it starts from TODAY's confirmed demand (gain >= 3%, volume >= 1.5x robust median, trend template intact, close in upper half of range) and shows structure context alongside. Ranked by ATR-normalized Quality Score so mid-cap moves aren't crushed by micro-cap squeezes. Tradeable-only ($20M+ ADV) toggle on by default.

**QUALITY** — IBD-style fundamentals scan. Composite rating, EPS rating, A/D rating, U/D volume ratio, forward EPS growth. Separate universe pass with its own cache.

**ROTATION** — industry group RS rankings and momentum deltas, with drill-down to see all members of a given industry.

**PERFORMANCE** — the trade journal. Tracks positions with adds, partial exits, stop moves (immutable initial-stop anchor for R-multiple calcs), realized vs. unrealized P&L, G/L%, Live Risk, Open R, days held, cost basis, and a per-trade Decision Snapshot panel. Dashboard tiles show Total P&L, Open P&L, Value, Live Risk, Total R.

**MORE** — dialog containing MEAN REVERSION (short-term oversold bounces in quality names), SOCIAL (Reddit mention sentiment via public JSON), and RESEARCH (deep-dive by ticker without going through a scan).

### Control Tower (top of Momentum)

Every Momentum view starts with the Control Tower, which shows:

- **MARKET SIGNAL** (GREEN/YELLOW/RED) — distribution-day summary across S&P 500, Nasdaq, Russell 2000
- **MARKET STATE** (UPTREND / RALLY_ATTEMPT / CORRECTION) — O'Neil-style state machine driven by SPY, with FTD detection, correction-low tracking, and fast-kill / slow-grind downgrade logic
- **UNIVERSE / >50MA / >200MA / UP/DN VOL / NEW HIGHS / CANDIDATES** tiles — breadth snapshot
- **INTERNAL BREADTH** (GREEN/YELLOW/RED) — composite breadth read with divergence warnings

The MARKET STATE and MARKET SIGNAL together tell you whether to be aggressive (confirmed UPTREND, GREEN signal), defensive (CORRECTION), or in wait-and-see mode (RALLY_ATTEMPT).

### BUY ZONE Cohort Tracker

Momentum has a **COHORT** button that opens a dialog tracking every BUY ZONE flag ever surfaced by the scanner, with forward returns pulled from the price cache. Answers the measurement question: "is the scanner actually finding leaders, or just noise?" Populates automatically as scans accumulate.

### Deep-dive page

Click any ticker in any table to see a per-ticker view with:

- Price chart with 50/150/200 MAs, volume, VCP contraction zones, optional Fibonacci retracement/extension overlays (toggleable)
- Trend template compliance check with Minervini criteria
- Key ratios + ownership panel
- Reddit mentions
- Cache age captions (so you know how fresh the data is)
- Contextual back button to return to whichever table you came from

---

## Data layer and reliability

The scanner uses Yahoo Finance via the `yfinance` library as the primary data source, with a Stooq secondary source as fallback. Both are free scrapers and both have reliability limits.

**What goes wrong:** Yahoo throttles aggressive IPs. Scraping 3,000+ tickers in a session, especially after a stale-cache gap (vacation, weekend), can trip their throttle. When throttled, yfinance returns empty DataFrames silently rather than raising errors — the scan runs but produces universe=0 and no candidates.

**What helps:**
- One scan per day, not more
- Don't compulsively hit Refresh Prices in the Journal tab
- If you've been away >3 days, expect the first scan post-return to take extra long (every ticker is cold-cache)
- If Yahoo throttles you, STOP scanning for 48 hours — every retry teaches their throttle score to keep you on the list longer

### Diagnostic tool

Included in the repo root: `diagnose_data.py`. Run it in your activated venv to test whether Yahoo and Stooq are reachable from your IP:

```
.venv\Scripts\activate
python diagnose_data.py
```

Output tells you whether each source is working, and based on the combination, what the likely cause of any scan failure is. Run this BEFORE restarting Streamlit for the fourth time out of frustration.

---

## Troubleshooting

**Scan returns 0 candidates (universe = 0 or very small)** — Yahoo Finance is throttling you. Don't rescan. Run `python diagnose_data.py` to confirm. If the diagnostic shows single AAPL calls work but your scan still returns zero, you've been throttled at bulk-request scale. Wait 24-48 hours before trying again. Each retry extends the throttle.

**Scan returns a tiny universe (e.g., universe = 11)** — Same cause as above, slightly less severe. The dashboard will show misleadingly healthy "Universe: 4,378" because that's the count of tickers REQUESTED; the actual pool that got real data is tiny. Check `industry_universe` rows in the raw scan file at `data/scans/latest.json` for the true count.

**Scan completes but Streamlit shows yesterday's data** — The rate-limit guardrail (`MIN_HEALTHY_UNIVERSE = 1500` in `scanner/cache.py`) rejected your scan from overwriting the previous healthy one. This is a feature, not a bug: it prevents a bad scan from corrupting your working dashboard. The flash message "Scan rejected — likely rate-limited" told you this. Wait and retry later.

**"python is not recognized"** — Python wasn't added to PATH during install. Uninstall Python and reinstall, this time checking the "Add python.exe to PATH" box on the first installer screen.

**"git is not recognized"** — Same issue with Git. Reinstall Git and pay attention to the PATH option screen.

**Streamlit won't start / "ModuleNotFoundError"** — Your venv isn't activated. From the `LB-stock-scanner` folder: `.venv\Scripts\activate` (you should see `(.venv)` in the prompt). Then try `run.bat` again.

**Browser shows "localhost refused to connect"** — Streamlit crashed on startup OR the server isn't up yet. Check the cmd window for an error message. If there's a traceback, screenshot it and send to the repo owner. If the window is still scrolling text, wait another 15 seconds and reload.

**Scan is extremely slow (>45 min on a day you've scanned recently)** — Yfinance per-ticker fallbacks are firing because the batched calls are returning empty. This happens as you approach the throttle threshold. Let it finish (don't kill it mid-scan), then stop scanning for a day.

**Mode button row looks weird or crowded** — Reload the page (F5). Streamlit occasionally mis-renders button widths after a tab switch.

---

## Known limitations and honest caveats

- **Yahoo Finance is not an SLA'd data source.** It's an undocumented scraper that works until it doesn't. For a scanner you rely on, this is the biggest architectural weakness. The repo owner may at some point migrate to a paid source (Tiingo, Polygon) — if so, there will be new setup steps for an API key.
- **EOD data only.** No intraday feed, no live quotes. The scan runs on close prices and the dashboard reflects whatever yfinance served the last time you scanned.
- **The signal thresholds are heuristics.** They're based on Minervini/O'Neil published rules plus personal tuning. They're not backtested in any rigorous way — treat them as filters, not guarantees.
- **No slippage/spread modeling.** Suggested buy triggers assume you can fill at the pivot price. In reality, breakouts gap and spreads widen. Discount accordingly.
- **No position-level autonomous actions.** The Journal tracks what you did; it doesn't place orders. You still have to execute at your broker manually.

---

## Questions?

Ping the repo owner. They've been through every weird edge case in this codebase (sometimes multiple times) and can help debug.
