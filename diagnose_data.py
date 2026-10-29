"""
Data source diagnostic — tests Yahoo Finance AND Stooq from your machine's IP.

Usage from the Stock Scanner folder:
    .venv\\Scripts\\activate
    python diagnose_data.py

Reports whether each source can serve fresh price data for a known-good ticker
(AAPL). If both fail, you're being throttled at the network level and need a
paid data provider. If Stooq works, the in-scanner fallback should also work
and something's wrong with the wiring.
"""
from __future__ import annotations

import sys
import time
import datetime as dt


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def test_yahoo() -> bool:
    """Try a fresh 30-day pull of AAPL via yfinance."""
    print("[1] Yahoo Finance (yfinance)...")
    try:
        import yfinance as yf
    except ImportError:
        _fail("yfinance not installed — run: pip install yfinance")
        return False

    start = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        t0 = time.time()
        df = yf.Ticker("AAPL").history(start=start, interval="1d")
        elapsed = time.time() - t0
        if df is None or df.empty:
            _fail(f"yfinance returned empty ({elapsed:.1f}s) — RATE LIMITED or blocked")
            return False
        _ok(f"got {len(df)} bars in {elapsed:.1f}s, last close ${float(df['Close'].iloc[-1]):.2f} on {df.index[-1].date()}")
        return True
    except Exception as e:
        _fail(f"exception: {type(e).__name__}: {e}")
        return False


def test_stooq() -> bool:
    """Try a fresh 30-day pull of AAPL via Stooq CSV endpoint."""
    print("[2] Stooq (secondary fallback)...")
    try:
        import requests  # noqa: F401
    except ImportError:
        _fail("requests not installed — run: pip install requests")
        return False

    try:
        from scanner.stooq import fetch_stooq_history
    except ImportError as e:
        _fail(f"can't import scanner.stooq: {e}")
        return False

    start = (dt.datetime.now() - dt.timedelta(days=30)).date()
    try:
        t0 = time.time()
        df = fetch_stooq_history("AAPL", start_date=start)
        elapsed = time.time() - t0
        if df is None or df.empty:
            _fail(f"Stooq returned empty/None ({elapsed:.1f}s) — BLOCKED FROM YOUR IP or endpoint changed")
            return False
        _ok(f"got {len(df)} bars in {elapsed:.1f}s, last close ${float(df['Close'].iloc[-1]):.2f} on {df.index[-1].date()}")
        return True
    except Exception as e:
        _fail(f"exception: {type(e).__name__}: {e}")
        return False


def test_stooq_raw() -> bool:
    """Bypass our wrapper and hit Stooq's raw endpoint to isolate wrapper issues."""
    print("[3] Stooq raw HTTP (isolate wrapper vs. connectivity)...")
    try:
        import requests
    except ImportError:
        _fail("requests missing (see above)")
        return False

    url = "https://stooq.com/q/d/l/?s=aapl.us&i=d"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers, timeout=10)
        elapsed = time.time() - t0
        if r.status_code != 200:
            _fail(f"HTTP {r.status_code} in {elapsed:.1f}s")
            return False
        body = r.text or ""
        if not body or len(body) < 200:
            _fail(f"empty/tiny response ({len(body)} bytes) in {elapsed:.1f}s — likely bot detection")
            return False
        first_line = body.split("\n", 1)[0][:80]
        _ok(f"got {len(body)} bytes in {elapsed:.1f}s")
        _ok(f"  first line: {first_line}")
        return True
    except Exception as e:
        _fail(f"exception: {type(e).__name__}: {e}")
        return False


def main() -> int:
    print("=" * 68)
    print("Stock Scanner — Data Source Diagnostic")
    print("=" * 68)
    print()

    yahoo_ok = test_yahoo()
    print()
    stooq_ok = test_stooq()
    print()
    raw_ok = test_stooq_raw()
    print()

    print("=" * 68)
    print("VERDICT")
    print("=" * 68)
    if yahoo_ok and stooq_ok:
        print("Both sources work. The scanner should run cleanly — if it's still")
        print("failing, the issue is inside the scan orchestration, not the network.")
    elif yahoo_ok and not stooq_ok:
        print("Yahoo works, Stooq is blocked. Fine — Yahoo alone is enough,")
        print("run the scanner normally. If it's still failing, the issue is")
        print("elsewhere (share the error and universe_size from data/scans/).")
    elif not yahoo_ok and stooq_ok:
        print("Yahoo is blocked but Stooq works. The Stooq fallback IN THE SCANNER")
        print("should carry your scans — if it's not, there's a wiring bug we")
        print("need to fix. Share this output with Claude.")
    else:
        print("BOTH sources are unreachable from your IP right now. Free data is")
        print("dead until whichever throttle lifts. Options:")
        print("  (a) Wait 4-8 hours and try this diagnostic again.")
        print("  (b) Try a VPN (ProtonVPN free tier works).")
        print("  (c) Switch to a paid data provider (Tiingo $10/mo, Polygon $30/mo).")
        print("       This is the durable fix — free scrapers were never a")
        print("       long-term solution for something you rely on nightly.")
    print()
    return 0 if (yahoo_ok or stooq_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
