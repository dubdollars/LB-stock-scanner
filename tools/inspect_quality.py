"""
Phase Q1 diagnostic — exercise scanner/quality.py end-to-end on 5 tickers.

Verifies:
  - Quality module imports cleanly
  - Per-ticker fetch returns expected fields (not all None)
  - Price-derived calcs work (U/D Vol, RS_3m, RS_6m, Acc/Dis) for tickers
    that have price history cached
  - Percentile rankings produce 1-99 values
  - Composite rating blends correctly
  - Result writes to data/scans/scan_quality_*.json successfully

Run from the Stock Scanner folder:
    .venv\\Scripts\\python.exe tools\\inspect_quality.py
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

_out = open("quality_diagnostic.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _out)

print("=" * 80)
print("Quality module Phase Q1 diagnostic")
print("=" * 80)

# Add project root to path (this script lives in tools/)
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
sys.path.insert(0, ".")

try:
    import yfinance as yf
    import pandas as pd
    from scanner.quality import (
        _fetch_one_quality,
        compute_ud_vol_ratio,
        compute_n_day_return,
        compute_acc_dis_rating,
        compute_composite_rating,
        _percentile_rank_to_1_99,
        run_quality_scan,
    )
    from scanner.data_cache import load_price_history
    print(f"yfinance {yf.__version__}, pandas {pd.__version__}")
    print("Quality module imported OK")
except Exception as e:
    print(f"IMPORT FAILED: {e}")
    traceback.print_exc()
    sys.exit(1)

# Tickers we know are in the price cache (used these in previous diagnostics)
TEST_TICKERS = ["AXTI", "MTSI", "FORM", "PAYS", "IRDM"]

print()
print("-" * 80)
print("STEP 1 — Per-ticker quality fundamentals fetch")
print("-" * 80)
fetched = {}
for t in TEST_TICKERS:
    print(f"\n  {t}:")
    try:
        r = _fetch_one_quality(t, True, True, True, True)
        fetched[t] = r
        for k in ("sector", "industry", "trailing_pe", "forward_pe",
                  "fwd_eps_growth_pct", "roe_pct", "inst_pct"):
            print(f"    {k}: {r.get(k)}")
    except Exception as e:
        print(f"    EXCEPTION: {e}")
        traceback.print_exc()

print()
print("-" * 80)
print("STEP 2 — Price-derived calculations (from cached prices/*.pkl)")
print("-" * 80)
ud_vols = {}
rets_3m = {}
rets_6m = {}
ads = {}
for t in TEST_TICKERS:
    df = load_price_history(t)
    if df is None or df.empty:
        print(f"  {t}: NO PRICE DATA in cache — skip")
        continue
    ud = compute_ud_vol_ratio(df, lookback=50)
    r3 = compute_n_day_return(df, 63)
    r6 = compute_n_day_return(df, 126)
    ad = compute_acc_dis_rating(df, weeks=13)
    ud_vols[t] = ud
    rets_3m[t] = r3
    rets_6m[t] = r6
    ads[t] = ad
    print(f"  {t}: U/D Vol={ud}  3mo return={r3:.2%} if r3 else 'N/A'  "
          .replace("if r3 else 'N/A'", "")
          + f"6mo return={r6:.2%}  Acc/Dis={ad}" if r3 is not None and r6 is not None
            else f"  {t}: U/D Vol={ud}  3mo={r3}  6mo={r6}  Acc/Dis={ad}")

print()
print("-" * 80)
print("STEP 3 — Universe-relative percentile rankings (just the 5 test tickers)")
print("-" * 80)
fwd_eps_growths = {t: fetched.get(t, {}).get("fwd_eps_growth_pct") for t in TEST_TICKERS}
eps_ratings = _percentile_rank_to_1_99(fwd_eps_growths, descending=True)
rs_3m_ratings = _percentile_rank_to_1_99(rets_3m, descending=True)
rs_6m_ratings = _percentile_rank_to_1_99(rets_6m, descending=True)
print(f"  fwd EPS growths: {fwd_eps_growths}")
print(f"  EPS Ratings (1-99): {eps_ratings}")
print(f"  3-month returns: " + ", ".join(f"{k}={v:.2%}" if v is not None else f"{k}=N/A" for k,v in rets_3m.items()))
print(f"  RS_3m (1-99):    {rs_3m_ratings}")
print(f"  RS_6m (1-99):    {rs_6m_ratings}")

print()
print("-" * 80)
print("STEP 4 — Composite Rating blend")
print("-" * 80)
# Simulate having RS_Rating + Group RS as inputs (since we don't have a real
# RS scan running, use the RS_3m as a proxy stand-in for the test)
fake_rs_map = {t: rs_3m_ratings.get(t) for t in TEST_TICKERS}
fake_group_rs = {t: 70.0 for t in TEST_TICKERS}  # neutral placeholder
for t in TEST_TICKERS:
    comp = compute_composite_rating(
        eps_ratings.get(t),
        fake_rs_map.get(t),
        ads.get(t),
        fake_group_rs.get(t),
    )
    print(f"  {t}: EPS={eps_ratings.get(t)} RS={fake_rs_map.get(t)} A/D={ads.get(t)} "
          f"Group={fake_group_rs.get(t)} -> Composite={comp}")

print()
print("-" * 80)
print("STEP 5 — Full run_quality_scan on the 5-ticker subset")
print("-" * 80)
def _prog(stage, completed, total, msg):
    print(f"  [PROGRESS] {stage}: {completed}/{total} {msg}")

try:
    result = run_quality_scan(
        tickers=TEST_TICKERS,
        progress=_prog,
        rs_map=fake_rs_map,
        group_rs_map=fake_group_rs,
    )
    print(f"\n  Scan complete: universe_size={result.universe_size}, candidates={len(result.candidates)}")
    print(f"  Diagnostics: {result.diagnostics}")
    print()
    print("  Top candidates (sorted by Composite Rating):")
    for c in result.candidates[:10]:
        print(f"    {c['Ticker']:<6}  Comp={c['Composite_Rating']:<3}  "
              f"EPS={c['EPS_Rating']:<3}  RS={c['RS_Rating']:<3}  "
              f"A/D={c['Acc_Dis_Rating']:<3}  U/D={c['UD_Vol_Ratio']:<5}  "
              f"TrPE={c['Trailing_PE']}  FwdPE={c['Forward_PE']}  Inst%={c['Inst_Pct']}")
except Exception as e:
    print(f"  SCAN FAILED: {e}")
    traceback.print_exc()

print()
print("=" * 80)
print("DONE. Output also saved to quality_diagnostic.txt")
print("=" * 80)
_out.close()
