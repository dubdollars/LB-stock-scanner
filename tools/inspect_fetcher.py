"""
Test the scanner's actual fetcher (_fetch_one_fundamentals) for 5 tickers.
This is the FUNCTION the scan is using; if it returns (None,None,None)
something inside it is raising. Capture the exception verbosely.

Run from the Stock Scanner folder:
    .venv\\Scripts\\python.exe inspect_fetcher.py
"""
import sys, io, traceback

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

_out = open("fetcher_diagnostic.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _out)

print("=" * 80)
print("Scanner fetcher direct test")
print("=" * 80)

# Import the actual scanner fetcher
sys.path.insert(0, ".")
try:
    from scanner.core import _fetch_one_fundamentals, safe_get_info, _compute_growth_flags
    import yfinance as yf
    import pandas as pd
    print(f"yfinance {yf.__version__}, pandas {pd.__version__}")
except Exception as e:
    print(f"IMPORT FAILED: {e}")
    traceback.print_exc()
    sys.exit(1)

TICKERS = ["AXTI", "MTSI", "FORM", "PAYS", "IRDM"]

for ticker in TICKERS:
    print()
    print("-" * 80)
    print(f"TICKER: {ticker}")
    print("-" * 80)

    # Step 1: try yf.Ticker(t).info directly (what safe_get_info wraps)
    print("\n  Step 1: yf.Ticker(t).info ...")
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if info is None:
            print(f"    info returned None")
        elif not isinstance(info, dict):
            print(f"    info returned non-dict: {type(info).__name__}")
        else:
            keys_of_interest = ["sector", "industry", "marketCap", "pegRatio",
                                "heldPercentInstitutions"]
            for k in keys_of_interest:
                v = info.get(k, "MISSING")
                print(f"    info['{k}'] = {v!r}")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Step 2: try safe_get_info (the scanner's wrapper)
    print("\n  Step 2: safe_get_info(t) ...")
    try:
        info2 = safe_get_info(t)
        if info2 is None:
            print(f"    safe_get_info returned None")
        else:
            print(f"    safe_get_info returned dict with {len(info2)} keys")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Step 3: try quarterly_financials directly
    print("\n  Step 3: t.quarterly_financials ...")
    try:
        df = t.quarterly_financials
        if df is None:
            print(f"    returned None")
        else:
            print(f"    returned df shape {df.shape}")
            if 'Net Income' in df.index:
                ni = df.loc['Net Income']
                print(f"    Net Income iloc[0..4]: {[ni.iloc[i] if i < len(ni) else 'N/A' for i in range(5)]}")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Step 4: try _compute_growth_flags
    print("\n  Step 4: _compute_growth_flags(quarterly_financials) ...")
    try:
        flags = _compute_growth_flags(df)
        print(f"    returned {flags}")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Step 5: full _fetch_one_fundamentals call (what the scanner does)
    print("\n  Step 5: _fetch_one_fundamentals(ticker, True, True, True) ...")
    try:
        result = _fetch_one_fundamentals(ticker, True, True, True)
        print(f"    returned tuple of len {len(result)}")
        labels = ["static_data", "mktcap_data", "growth_data"]
        for label, val in zip(labels, result):
            if val is None:
                print(f"    {label}: None")
            else:
                print(f"    {label}: dict with keys {sorted(val.keys())}")
                if label == "growth_data":
                    print(f"      fund_display: {val.get('fund_display')!r}")
                    print(f"      eps_flag: {val.get('eps_flag')}")
                    print(f"      rev_flag: {val.get('rev_flag')}")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()

print()
print("=" * 80)
print("DONE. Output also saved to fetcher_diagnostic.txt")
print("=" * 80)
_out.close()
