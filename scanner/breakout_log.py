"""
Breakout Log — persistent tracking of every ticker that fired BO TODAY.

Purpose
-------
The RETEST signal (second-entry Minervini setup) needs to know which tickers
broke out recently, at what pivot, and whether price has held the breakout.
That requires MEMORY across scans — a single scan sees today's snapshot only,
so we log every breakout as it happens and query the log on future scans.

A "breakout retest" fires when:
  1. Ticker broke out on some prior day (in the log)
  2. That breakout was 3-15 trading sessions ago
  3. Price has held above the breakout pivot the entire time (no fill)
  4. Today, price is within 3% of the original pivot (pulled back cleanly)
  5. Recent volume is quiet (last 5 sessions below 20d avg)

Behavior mirrors buy_zone_cohort.py: append-only JSONL, dedupe on
(breakout_date, ticker), silent skip on bad rows. Written to the same
data/ directory alongside journal.json and buy_zone_cohort.jsonl.

File layout
-----------
    data/breakout_log.jsonl   # one entry per line, JSON-encoded

Each row's schema:

    {
      "breakout_date":     "2026-07-22",       # YYYY-MM-DD
      "logged_at":         "2026-07-22T15:34Z",# ISO scan timestamp
      "ticker":            "NVDA",
      "breakout_close":    132.85,             # close on breakout day
      "breakout_pivot":    130.50,             # Buy_Trigger the day it broke
      "breakout_stop":     124.00,             # Stop the day it broke
      "breakout_vol_ratio":1.82,               # today's vol / 50d avg
      "breakout_market_signal":"YELLOW",       # regime at breakout
      "breakout_sector":   "Technology",
      "breakout_industry": "Semiconductors",
    }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

BREAKOUT_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "breakout_log.jsonl"
)


def _ensure_dir() -> None:
    BREAKOUT_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_breakouts() -> list[dict]:
    """Load every breakout entry. Silent-skip corrupted rows.

    Returns entries sorted NEWEST FIRST. Empty list if file doesn't
    exist or has no valid rows.
    """
    _ensure_dir()
    if not BREAKOUT_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with BREAKOUT_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    out.sort(
        key=lambda r: (r.get("breakout_date", ""), r.get("ticker", "")),
        reverse=True,
    )
    return out


def get_breakout_map(within_days: int = 30) -> dict[str, dict]:
    """Return {ticker → most recent breakout entry} for the last N calendar days.

    RETEST detection needs to know for each ticker: was there a recent
    breakout, and if so what was its pivot? This map answers that in O(1)
    per ticker without re-scanning the file. Only the MOST RECENT breakout
    per ticker is kept — if a ticker has broken out multiple times, we care
    about the freshest one.

    within_days is a soft cap — the retest signal itself is scoped to 3-15
    trading days, but we pull a wider window (30 calendar days) to be safe
    across weekends/holidays.
    """
    try:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=within_days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = ""
    out: dict[str, dict] = {}
    for row in load_breakouts():  # already newest-first
        t = row.get("ticker")
        if not t:
            continue
        if row.get("breakout_date", "") < cutoff:
            break  # older than window — since sorted desc, nothing newer follows
        if t not in out:
            out[t] = row
    return out


def _existing_keys() -> set[tuple[str, str]]:
    """Set of (breakout_date, ticker) already logged — used for dedup."""
    return {
        (r.get("breakout_date", ""), r.get("ticker", ""))
        for r in load_breakouts()
    }


def append_breakout_snapshot(
    candidates: Iterable[dict],
    market_signal: str,
    scan_timestamp: Optional[str] = None,
) -> int:
    """Append every candidate whose Signals contain "BO TODAY" to the log.

    Called from save_scan after a Momentum scan. Idempotent — dedupe key is
    (breakout_date, ticker). Running two scans on the same day doesn't
    double-log.

    Returns
    -------
    int  Number of NEW rows appended (excludes duplicates).
    """
    _ensure_dir()
    if scan_timestamp is None:
        scan_timestamp = datetime.utcnow().isoformat() + "Z"

    # Derive flag date from scan timestamp (YYYY-MM-DD prefix).
    breakout_date = str(scan_timestamp)[:10]
    existing = _existing_keys()

    written = 0
    with BREAKOUT_FILE.open("a", encoding="utf-8") as f:
        for c in candidates:
            signals = c.get("Signals") or []
            # Support both list form and " · "-joined string form.
            if isinstance(signals, str):
                signals_iter = [s.strip() for s in signals.split("·")]
            else:
                signals_iter = list(signals)
            if "BO TODAY" not in signals_iter:
                continue
            ticker = c.get("Ticker")
            if not ticker:
                continue
            if (breakout_date, ticker) in existing:
                continue

            row = {
                "breakout_date":         breakout_date,
                "logged_at":             scan_timestamp,
                "ticker":                ticker,
                "breakout_close":        c.get("Price"),
                "breakout_pivot":        c.get("Buy_Trigger"),
                "breakout_stop":         c.get("Stop"),
                "breakout_vol_ratio":    c.get("Vol_Ratio_50"),
                "breakout_market_signal": market_signal,
                "breakout_sector":       c.get("Sector"),
                "breakout_industry":     c.get("Industry"),
            }
            f.write(json.dumps(row, default=str) + "\n")
            existing.add((breakout_date, ticker))
            written += 1
    return written
