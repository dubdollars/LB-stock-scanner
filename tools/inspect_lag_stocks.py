"""
Targeted diagnostic: dump full Net Income and Total Revenue values for
5 stocks currently flagged "Lag" and 5 stocks currently flagged "EPS+/Sale+"
in the local cache. Show all five quarters and run the growth calculation
the scanner uses, so we can see WHY the parser flags what it does.

Run from the Stock Scanner folder:
    .venv\\Scripts\\python.exe inspect_lag_stocks.py
"""

import sys, json, io

class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try: st.write(s); st.flush()
            except Exception: pass
    def flush(self):
        for st in self.streams:
            try: st.flush()
            except Exception: pass

_out_file = open("lag_diagnostic.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _out_file)

print("=" * 80)
print("LAG vs EPS+ targeted diagnostic")
print("=" * 80)

try:
    import yfinance as yf
    import pandas as pd
    print(f"yfinance {yf.__version__}, pandas {pd.__version__}")
except Exception as e:
    print(f"FAILED to import: {e}")
    sys.exit(1)

# Load the cache to find Lag vs EPS+ stocks
CACHE_PATH = "data/cache/fundamentals.json"
try:
    raw = open(CACHE_PATH, "rb").read().replace(b"\x00", b"")
    cache = json.loads(raw)
except Exception as e:
    print(f"FAILED to read cache at {CACHE_PATH}: {e}")
    sys.exit(1)

lag_tickers = []
good_tickers = []
for t, e in cache.items():
    if not isinstance(e, dict):
        continue
    fd = e.get("fund_display")
    if fd == "Lag" and len(lag_tickers) < 5:
        lag_tickers.append(t)
    elif fd and ("EPS+" in fd or "Sale+" in fd) and len(good_tickers) < 5:
        good_tickers.append(t)
    if len(lag_tickers) >= 5 and len(good_tickers) >= 5:
        break

print(f"\nSampled Lag stocks:  {lag_tickers}")
print(f"Sampled Pass stocks: {good_tickers}")

def _analyze(ticker, expected):
    print()
    print("-" * 80)
    print(f"TICKER: {ticker}  (cache says: {expected})")
    print("-" * 80)
    try:
        t = yf.Ticker(ticker)
        df = t.quarterly_financials
    except Exception as exc:
        print(f"  EXCEPTION fetching: {exc}")
        return

    if df is None or df.empty:
        print("  EMPTY DataFrame returned")
        return

    print(f"  shape: {df.shape}")
    cols = list(df.columns)
    print(f"  columns (oldest..newest? or newest..oldest?): {cols}")

    for row_name in ["Net Income", "Total Revenue", "Diluted EPS"]:
        print()
        if row_name not in df.index:
            print(f"  '{row_name}' row NOT FOUND in index")
            # Show what IS in index that looks similar
            similar = [k for k in df.index if row_name.lower().replace(" ","")
                       in str(k).lower().replace(" ","")]
            if similar:
                print(f"    similar labels in index: {similar}")
            continue

        row = df.loc[row_name]
        print(f"  '{row_name}' values across {len(row)} quarters:")
        for i, (col, val) in enumerate(row.items()):
            print(f"    iloc[{i}]  {col.date() if hasattr(col,'date') else col}: {val}")

        # Now run the SAME math the scanner uses
        clean = row.dropna()
        print(f"  after dropna: {len(clean)} non-null values")
        if len(clean) >= 5 and clean.iloc[4] != 0:
            growth = (clean.iloc[0] - clean.iloc[4]) / abs(clean.iloc[4])
            threshold = 0.25 if row_name == "Net Income" else 0.20
            verdict = "PASS" if growth > threshold else "FAIL"
            print(f"  YoY growth (Q0 vs Q4): {growth*100:.1f}%  [threshold {threshold*100:.0f}%]  -> {verdict}")
        elif len(clean) >= 2 and clean.iloc[-1] != 0:
            growth = (clean.iloc[0] - clean.iloc[-1]) / abs(clean.iloc[-1])
            threshold = 0.25 if row_name == "Net Income" else 0.20
            verdict = "PASS" if growth > threshold else "FAIL"
            print(f"  fallback newest-vs-oldest growth: {growth*100:.1f}%  [threshold {threshold*100:.0f}%]  -> {verdict}")
        else:
            print(f"  insufficient data: cannot compute growth (need >=5 quarters or fallback)")

print("\n" + "=" * 80)
print("CACHED AS LAG (expecting parser to confirm <25% growth)")
print("=" * 80)
for t in lag_tickers:
    _analyze(t, "Lag")

print("\n" + "=" * 80)
print("CACHED AS EPS+/Sale+ (expecting parser to confirm >25% growth)")
print("=" * 80)
for t in good_tickers:
    _analyze(t, "EPS+/Sale+")

print("\n" + "=" * 80)
print("DONE. Output also saved to lag_diagnostic.txt")
print("=" * 80)
_out_file.close()
