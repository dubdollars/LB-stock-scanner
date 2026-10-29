"""
Stooq data-source fallback (added 2026-08-07).

Purpose
-------
Yahoo Finance rate-limits retail IPs, especially at night. When yfinance
returns empty DataFrames for large chunks of the ticker universe, the
scanner's rate-limit guardrail rejects the whole scan and the user is
locked out for hours. Stooq is a free European stock-data mirror with
almost no throttling on retail volume — perfect as a secondary source.

Contract
--------
Given a ticker + optional start date, return a pandas DataFrame with
columns [Open, High, Low, Close, Volume] and a DatetimeIndex. Same shape
yfinance returns after column-level flattening. If Stooq has no data or
the fetch fails, return None. Caller treats that identically to a
yfinance miss (skip the ticker).

Ticker translation
------------------
Stooq uses lowercase + ".us" suffix for US-listed common stocks:
    AAPL     → aapl.us
    BRK-B    → brk-b.us      (some pkgs use brk.b — brk-b works too)
    RDS.A    → rds.a.us
Any ticker with a dash is preserved as-is (Stooq accepts either form).

Usage
-----
    from scanner.stooq import fetch_stooq_history, fetch_stooq_batch

    df = fetch_stooq_history("AAPL", start_date=some_date)
    if df is not None:
        # merge into stock_cache

    results = fetch_stooq_batch(["AAPL","MSFT","BAD"], start_date, workers=4)
    # {"AAPL": df, "MSFT": df}   — BAD absent because fetch returned None
"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import pandas as pd
import requests


# Base CSV endpoint. `i=d` = daily interval. `d1`/`d2` optionally bound the
# range as YYYYMMDD. Omitting them returns Stooq's full history for the
# symbol (~20 years for well-known names).
_STOOQ_URL = "https://stooq.com/q/d/l/"

# Polite headers — Stooq responds to any real browser UA. Bot-shaped requests
# occasionally get bounced with an empty body, so mimic a Chrome install.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Per-request timeout. Stooq usually responds in <2s; if 10s isn't enough,
# it's degenerate and we should give up rather than hang the scan.
_TIMEOUT = 10.0


def _to_stooq_symbol(ticker: str) -> str:
    """AAPL → aapl.us. Preserves dashes (Stooq handles both dash and dot)."""
    return f"{ticker.strip().lower()}.us"


def fetch_stooq_history(
    ticker: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    session: Optional[requests.Session] = None,
) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV from Stooq. Returns DataFrame or None on any failure.

    The DataFrame matches yfinance's schema after flattening:
      * DatetimeIndex named 'Date'
      * Columns: Open, High, Low, Close, Volume  (Adj Close not provided;
        Stooq returns split-adjusted closes by default so no need)
    """
    if not ticker or not isinstance(ticker, str):
        return None

    symbol = _to_stooq_symbol(ticker)
    params = {"s": symbol, "i": "d"}
    if start_date is not None:
        params["d1"] = start_date.strftime("%Y%m%d")
    if end_date is not None:
        params["d2"] = end_date.strftime("%Y%m%d")

    try:
        s = session or requests
        r = s.get(_STOOQ_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception:
        return None

    if r.status_code != 200:
        return None
    body = r.text or ""
    # Stooq returns "No data" (verbatim) or just an empty body when the
    # symbol is unknown or throttled. Guard both.
    if not body or len(body) < 40 or body.strip().lower().startswith("no data"):
        return None

    try:
        df = pd.read_csv(io.StringIO(body))
    except Exception:
        return None

    # Expected columns: Date, Open, High, Low, Close, Volume
    if "Date" not in df.columns or "Close" not in df.columns:
        return None

    try:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"])
        df = df.set_index("Date").sort_index()
    except Exception:
        return None

    # Coerce numeric columns; drop rows where the essential fields aren't
    # numbers (defends against occasional malformed rows at the top of a
    # Stooq feed for very new IPOs).
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Close"])

    if df.empty:
        return None

    # Match yfinance's tz-naive DatetimeIndex convention (the rest of the
    # scanner assumes naive dates for cache alignment).
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
    except Exception:
        pass

    return df


def fetch_stooq_batch(
    tickers: Iterable[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    workers: int = 4,
    progress_cb=None,
) -> dict[str, pd.DataFrame]:
    """Fetch many tickers in parallel. Returns {ticker: DataFrame}.

    Silently omits any ticker whose fetch returned None. `workers` kept
    modest (4) so we stay well inside Stooq's tolerated request rate —
    they're friendly but not infinite. `progress_cb(done, total, current)`
    fires after each completion for scanner progress-bar wiring.
    """
    tickers = [t for t in (t.strip().upper() for t in tickers if t) if t]
    out: dict[str, pd.DataFrame] = {}
    if not tickers:
        return out

    # Reuse a Session so the TCP + TLS handshake amortizes across requests.
    session = requests.Session()
    session.headers.update(_HEADERS)

    total = len(tickers)
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        future_to_ticker = {
            pool.submit(fetch_stooq_history, t, start_date, end_date, session): t
            for t in tickers
        }
        for fut in as_completed(future_to_ticker):
            t = future_to_ticker[fut]
            try:
                df = fut.result()
            except Exception:
                df = None
            if df is not None and not df.empty:
                out[t] = df
            done += 1
            if progress_cb:
                try:
                    progress_cb(done, total, t)
                except Exception:
                    pass

    try:
        session.close()
    except Exception:
        pass

    return out
