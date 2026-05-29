"""
Test the scanner's PARALLEL orchestrator directly with a 5-ticker subset.
The orchestrator is fetch_fundamentals_sector_mktcap. If it returns 0
fetched, the orchestrator itself is broken. If it returns 5 fetched,
the bug is upstream of the orchestrator (somewhere in run_scan).

Run from the Stock Scanner folder:
    .venv\\Scripts\\python.exe inspect_orchestrator.py
"""
import sys, io, traceback, json

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

_out = open("orchestrator_diagnostic.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _out)

print("=" * 80)
print("Orchestrator direct test (parallel fetcher)")
print("=" * 80)

sys.path.insert(0, ".")
try:
    from scanner.core import fetch_fundamentals_sector_mktcap, _fetch_one_fundamentals
    from scanner.data_cache import load_fundamentals_cache
    import yfinance as yf
    import pandas as pd
    print(f"yfinance {yf.__version__}, pandas {pd.__version__}")
except Exception as e:
    print(f"IMPORT FAILED: {e}")
    traceback.print_exc()
    sys.exit(1)

# Build a minimal "stock_cache" — just need ticker keys, values can be empty dicts.
# The orchestrator iterates over stock_cache.keys() to know what to fetch.
TICKERS = ["AXTI", "MTSI", "FORM", "PAYS", "IRDM"]
stock_cache = {t: {} for t in TICKERS}

print(f"\nCalling fetch_fundamentals_sector_mktcap with {len(stock_cache)} tickers...")
print(f"Tickers: {TICKERS}\n")

# Progress callback to see what's happening
def _prog(stage, completed, total, msg):
    print(f"  [PROGRESS] {stage}: {completed}/{total} {msg}")

try:
    result = fetch_fundamentals_sector_mktcap(stock_cache, progress=_prog)
    print("\n  Orchestrator returned successfully")
    fund_map, sector_map, mktcap_map, earnings_map, peg_map, inst_map = result
    print("\n  Per-ticker results:")
    for t in TICKERS:
        fd = fund_map.get(t, "MISSING")
        sec = sector_map.get(t, "MISSING")
        mc = mktcap_map.get(t, "MISSING")
        ed = earnings_map.get(t, "MISSING")
        peg = peg_map.get(t, "MISSING")
        inst = inst_map.get(t, "MISSING")
        print(f"    {t}: fund={fd!r}  sector={sec}  mktcap={mc}  earn={ed}  peg={peg}  inst={inst}")
except Exception as e:
    print(f"\n  ORCHESTRATOR RAISED: {type(e).__name__}: {e}")
    traceback.print_exc()

# Inspect the cache that was just written
print("\nCache state after orchestrator call:")
cache = load_fundamentals_cache()
for t in TICKERS:
    e = cache.get(t, {})
    if not e:
        print(f"  {t}: NOT IN CACHE")
        continue
    print(f"  {t}: fund_display={e.get('fund_display','MISSING')!r}  "
          f"growth_cached_at={e.get('growth_cached_at','NEVER')}  "
          f"static_cached_at={e.get('static_cached_at','NEVER')}")

print()
print("=" * 80)
print("DONE. Output also saved to orchestrator_diagnostic.txt")
print("=" * 80)
_out.close()
