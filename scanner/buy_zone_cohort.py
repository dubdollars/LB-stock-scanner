"""
BUY ZONE Cohort Log — persistent tracking of every ticker flagged as BUY ZONE.

Purpose
-------
The scanner's job is to compress ~3,000 tickers into a short list of high-quality
candidates. But how do we know whether that list actually contains the winners?
Without measuring the forward performance of every BUY ZONE flag (not just the
ones the user happens to take a trade on), we can't answer:

    "Is the scanner finding real leaders? Or is it putting me on the wrong scent?"

This module solves that. Every time a Momentum scan runs, we snapshot the current
BUY ZONE candidates into an append-only JSONL log at `data/buy_zone_cohort.jsonl`.
Each row captures the flag context (price, pivot, stop, market signal, VCP grade,
etc.) so we can later compute forward returns and roll up cohort-level stats:

    • Hit rate at +1R / +2R
    • Average return at 5 / 10 / 20 sessions after flag
    • Regime-conditional stats (BUY ZONE hits during RED signal vs GREEN)
    • Sector-composition of daily cohorts (defensive tilt = bearish tell)

The file is append-only (one JSON object per line) so writes are atomic even
across concurrent scans, and old rows never move or get rewritten. Uniqueness is
enforced on (flag_date, ticker) — a ticker flagged on the same date twice
(e.g., user runs Momentum scan twice in a session) shows up only once.

File layout
-----------
    data/buy_zone_cohort.jsonl   # one entry per line, JSON-encoded

Each row's schema:

    {
      "flag_date":         "2026-07-02",      # YYYY-MM-DD, scan day
      "flagged_at":        "2026-07-02T15:34:12Z",  # ISO timestamp of scan
      "ticker":            "CGNX",
      "flag_close":        70.72,
      "flag_pivot":        68.44,             # Buy_Trigger from scan row
      "flag_stop":         66.50,
      "flag_market_signal":"RED",             # e.g. "RED"/"GREEN"/"YELLOW"
      "flag_pressure_20d": 1.55,
      "flag_pct_above_50ma":11.9,
      "flag_vcp_quality":  "B",
      "flag_avg_dollar_vol":85000000,
      "flag_sector":       "Technology",
      "flag_industry":     "Scientific Instruments",
      "flag_market_cap":   "$5.2B",           # pre-formatted from scan
    }
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

# One JSONL under data/ — sits next to journal.json and scans/ for a
# consistent "user-persistent data" layout.
COHORT_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "buy_zone_cohort.jsonl"
)


def _ensure_dir() -> None:
    COHORT_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_cohort() -> list[dict]:
    """Load every cohort entry as a list of dicts, newest-first.

    Returns [] if the file doesn't exist yet or a parse error occurs on any
    single line (bad lines are skipped, not fatal — the rest of the log is
    still usable).
    """
    _ensure_dir()
    if not COHORT_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with COHORT_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # Skip bad lines rather than aborting the whole load.
                    # A single corrupted row shouldn't nuke months of data.
                    continue
    except Exception:
        return []
    # Newest first — sort by flag_date desc, then ticker asc as tiebreak.
    out.sort(key=lambda r: (r.get("flag_date", ""), r.get("ticker", "")),
             reverse=True)
    return out


def _existing_keys() -> set[tuple[str, str]]:
    """Set of (flag_date, ticker) pairs already logged. Used to dedupe."""
    return {
        (r.get("flag_date", ""), r.get("ticker", ""))
        for r in load_cohort()
    }


def append_buy_zone_snapshot(
    candidates: Iterable[dict],
    market_signal: str,
    scan_timestamp: Optional[str] = None,
) -> int:
    """Append every candidate whose Signals contain "BUY ZONE" to the cohort log.

    Called from save_scan() after a Momentum scan completes. Idempotent — if
    a (flag_date, ticker) pair is already logged, that row is skipped rather
    than duplicated. That way running two scans on the same day doesn't
    double-count the cohort.

    Parameters
    ----------
    candidates : iterable of scan-row dicts
        The full candidate list from the just-completed scan.
    market_signal : str
        The scan's market_signal field (e.g., "RED (Heavy Distribution)").
        Stored on each row so we can later ask "what's the win rate on flags
        made during RED-signal days vs GREEN?"
    scan_timestamp : str or None
        ISO-format scan timestamp. If None, uses the current UTC time.

    Returns
    -------
    int
        Number of NEW rows appended (excludes ones that were already logged).
    """
    _ensure_dir()
    if scan_timestamp is None:
        scan_timestamp = datetime.utcnow().isoformat() + "Z"

    # Parse the flag date out of the scan timestamp. Format is
    # "2026-07-02T15:34:12.123456Z" → we want "2026-07-02".
    flag_date = scan_timestamp[:10]

    existing = _existing_keys()
    new_rows: list[dict] = []

    for c in candidates:
        signals = c.get("Signals") or []
        # Signals is a list of strings; check whether BUY ZONE is one of them.
        if not isinstance(signals, list) or "BUY ZONE" not in signals:
            continue

        ticker = c.get("Ticker") or ""
        if not ticker:
            continue

        key = (flag_date, ticker)
        if key in existing:
            continue  # already logged — dedupe

        # Extract just the top-line word from the market signal for concise
        # storage. "RED (Heavy Distribution)" → "RED". Keeps rollups clean.
        signal_short = (market_signal or "").split(" ")[0].strip().upper() or "UNKNOWN"

        row = {
            "flag_date":            flag_date,
            "flagged_at":           scan_timestamp,
            "ticker":               ticker,
            "flag_close":           c.get("Price") or c.get("Close"),
            "flag_pivot":           c.get("Buy_Trigger"),
            "flag_stop":            c.get("Stop"),
            "flag_market_signal":   signal_short,
            "flag_pressure_20d":    c.get("Pressure_20d"),
            "flag_pct_above_50ma":  c.get("Pct_Above_50MA"),
            "flag_vcp_quality":     c.get("VCP_Quality"),
            "flag_avg_dollar_vol":  c.get("Avg_Dollar_Volume_20d"),
            "flag_sector":          c.get("Sector"),
            "flag_industry":        c.get("Industry"),
            "flag_market_cap":      c.get("Market_Cap"),
        }
        new_rows.append(row)
        existing.add(key)

    if not new_rows:
        return 0

    # Append-only write — never touches existing rows. Even if the process
    # crashes mid-write, the previous log is intact (only some of the new
    # rows may be missing, which will be re-added on the next scan).
    with COHORT_FILE.open("a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, default=str) + "\n")

    return len(new_rows)


def cohort_summary() -> dict:
    """Quick top-line stats over the whole log for the UI tracker header.

    Returns { total, unique_tickers, oldest, newest }.
    """
    entries = load_cohort()
    if not entries:
        return {"total": 0, "unique_tickers": 0, "oldest": None, "newest": None}
    tickers = {r.get("ticker") for r in entries if r.get("ticker")}
    dates = [r.get("flag_date") for r in entries if r.get("flag_date")]
    return {
        "total":          len(entries),
        "unique_tickers": len(tickers),
        "oldest":         min(dates) if dates else None,
        "newest":         max(dates) if dates else None,
    }
