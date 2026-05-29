"""
Diagnostic: show exactly what yfinance is returning for quarterly_financials
and quarterly_income_stmt, so we can fix the parser from real data.

Run this once from the Stock Scanner folder:
    python inspect_yfinance.py

It will print to console AND save the output to yfinance_diagnostic.txt
which you can share for the fix.
"""

import sys
import io

# Tee output to both console and file
class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            st.write(s)
            st.flush()
    def flush(self):
        for st in self.streams:
            st.flush()

_out_file = open("yfinance_diagnostic.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _out_file)

print("=" * 70)
print("yfinance diagnostic")
print("=" * 70)

try:
    import yfinance as yf
    print(f"yfinance version: {yf.__version__}")
except Exception as e:
    print(f"FAILED to import yfinance: {e}")
    raise SystemExit(1)

import pandas as pd
print(f"pandas version:   {pd.__version__}")

# Representative sample: large-cap tech, mid-cap, small-cap, established
TICKERS = ["NVDA", "AAPL", "AMD", "MU", "TWLO"]

for ticker in TICKERS:
    print()
    print("=" * 70)
    print(f"TICKER: {ticker}")
    print("=" * 70)

    try:
        t = yf.Ticker(ticker)
    except Exception as e:
        print(f"  Failed to create Ticker: {e}")
        continue

    for attr in ("quarterly_financials", "quarterly_income_stmt", "quarterly_incomestmt"):
        print()
        print(f"--- {attr} ---")
        try:
            df = getattr(t, attr, None)
            if df is None:
                print("  (attribute returned None)")
                continue
            if not isinstance(df, pd.DataFrame):
                print(f"  (not a DataFrame, got {type(df).__name__})")
                continue
            if df.empty:
                print("  (empty DataFrame)")
                continue

            print(f"  shape: {df.shape}  (rows x cols)")
            print(f"  columns (dates): {list(df.columns)}")
            print(f"  index (row labels): there are {len(df.index)} rows")
            for label in df.index:
                # Show the label exactly as yfinance returns it
                row = df.loc[label]
                non_null = row.dropna()
                if len(non_null) >= 1:
                    sample = non_null.iloc[0]
                else:
                    sample = "(all null)"
                print(f"    {label!r}  -> {len(non_null)}/{len(row)} non-null, sample: {sample}")
        except Exception as e:
            print(f"  EXCEPTION accessing {attr}: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("DONE. Output also saved to yfinance_diagnostic.txt")
print("=" * 70)

_out_file.close()
