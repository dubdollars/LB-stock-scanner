# Minervini SEPA Scanner

A local dashboard wrapping your Minervini/O'Neil SEPA scanner. Dark Bloomberg/TradingView aesthetic, one-click rescan, and a deep-dive view for every flagged stock with charts, RS line analysis, fundamentals, and a pre-computed trade execution ticket.

---

## One-time setup

### 1. Install Python

Download Python **3.11 or newer** from:

  **<https://www.python.org/downloads/>**

⚠️ **During install, CHECK the box that says "Add python.exe to PATH".** Without this, the launcher won't find Python.

### 2. Launch the app

Double-click **`run.bat`** in this folder.

The first run will:
- Create a virtual environment (`.venv/` folder)
- Install all dependencies from `requirements.txt` (~3-5 minutes, one time only)
- Start the dashboard
- Open your browser to <http://localhost:8501>

Future launches skip setup and start in a few seconds.

---

## Daily use

1. Double-click **`run.bat`**.
2. Browser opens to the dashboard.
3. The **last cached scan** loads instantly. If it's stale, click **▶ Rescan Now** in the sidebar (~25 min for the full S&P 500 + Nasdaq-100 + Russell 2000 universe).
4. Click any row in the candidates table to **deep-dive** that stock — charts, RS line, fundamentals, trade ticket.

When you're done, just close the browser tab and press `Ctrl+C` in the black launcher window (or just close it).

---

## What's in the dashboard

### Top strip — Market context (the "M" factor)
Color-coded GREEN / YELLOW / RED banner showing the current Minervini market signal, with S&P 500, Nasdaq, and Russell 2000 trend status and distribution-day counts.

### Breadth metrics row
- Universe size scanned
- % of stocks above 50-day and 200-day MAs
- Up/down volume ratio
- New highs vs new lows
- Internal breadth status

### Candidates table
Sortable, filterable list of stocks that passed the SEPA screen. Columns:
- **RS** — Relative Strength rating (1-99)
- **RSI** — 14-day RSI
- **RS_NH** — RS line at new 50-day high (leadership signal)
- **VCP** — Volatility contraction detected
- **VDU** — Volume dry-up
- **Squat** — Bearish reversal candle (avoid these)
- **Fundamentals** — EPS+ / EPS++ / Sale+ tags
- **Buy >** / **Stop <** / **Risk %** / **Shares** — Pre-computed trade execution

Click any row → deep dive.

### Deep dive (per stock)
Four tabs:
- **📈 Price & VCP** — Candlestick + 50/150/200 SMAs, volume bars, buy/stop horizontal lines, VCP contraction zones highlighted
- **📊 RS Line** — Relative strength vs SPY with new-high markers
- **💰 Fundamentals** — 8 quarters of net income & revenue with YoY growth, profit/operating margins
- **🎯 Execution** — Trade ticket, R-multiple targets, sector context, next earnings date

---

## Sidebar settings

| Setting | What it does |
|---|---|
| **Elite mode** | RS≥85, VDU+EPS required (vs RS≥70 standard) |
| **Account size** | Used for share count calculation |
| **Risk per trade** | % of account risked per position |
| **Min price** | Filter out cheap stocks |
| **Min avg volume** | Liquidity filter |

Changes apply on the next scan.

---

## Files

```
Stock Scanner/
├── app.py                  ← Streamlit entry point
├── run.bat                 ← Double-click launcher
├── requirements.txt
├── README.md
├── scanner/
│   ├── core.py             ← Scan pipeline (refactored from your notebook)
│   ├── deepdive.py         ← Per-stock data fetcher
│   └── cache.py            ← Save/load scan results
├── ui/
│   ├── style.py            ← Dark theme CSS
│   └── deepdive_view.py    ← Charts and panels
└── data/scans/             ← Cached scan results (kept: last 30)
```

---

## Demo mode

The sidebar has a **⚡ Demo (~2 min)** button that runs the scan against just 20 well-known mega-cap tickers. Use this on first launch to verify everything works without waiting 25 minutes.

---

## Troubleshooting

**"Python is not installed or not on PATH"** when launching `run.bat`
→ You either didn't install Python, or didn't check "Add to PATH" during install. Reinstall from <https://www.python.org/downloads/> with that box checked.

**Dependency install fails**
→ Open a Command Prompt in this folder and run `pip install -r requirements.txt` manually to see the full error.

**Scan is very slow / timing out**
→ Yahoo Finance occasionally rate-limits. Try again later. The cached results from your last successful scan still load instantly.

**Browser doesn't open**
→ Manually navigate to <http://localhost:8501> in any browser.
