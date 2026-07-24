"""
Cohort hit-rate snapshot log.

Every time the BUY ZONE Cohort Tracker dialog opens, we append the current
top-line stats to a jsonl file at data/scans/cohort_history.jsonl. This gives
us a time-series of hit-rate evolution so the dialog can:

  1. Show a "vs previous day" delta on each tile (is today better than
     the last time I looked?)
  2. Render a line chart of hit rates over the last 30-60 days (is the
     regime trending up or down?)

Snapshots are append-only for auditability. When we read the history for
display, we dedupe by DATE (keeping the latest snapshot each day) so
opening the dialog 5 times in one day doesn't stuff the chart with 5 dots
at the same X value.

Format per line:
  {
    "timestamp": "2026-07-24T12:34:56Z",
    "date":      "2026-07-24",
    "n_total":   73,
    "n_mature":  46,
    "n_developing": 27,
    "n_decided": 2,
    "hit_rate_2r":   0.0,
    "hit_max_5pct":  23.9,
    "hit_max_10pct": 6.5
  }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH  = _REPO_ROOT / "data" / "scans" / "cohort_history.jsonl"


def _now_utc() -> datetime:
    return datetime.utcnow()


def append_snapshot(stats: dict) -> Optional[Path]:
    """Append a single snapshot line to the cohort history log.

    Silent no-op on any failure — never let the log break the dialog.
    Accepts the stats dict returned by _rollup_stats() in ui/cohort_view.py.
    """
    try:
        now = _now_utc()
        record = {
            "timestamp":       now.isoformat() + "Z",
            "date":            now.strftime("%Y-%m-%d"),
            "n_total":         int(stats.get("n_total") or 0),
            "n_mature":        int(stats.get("n_mature") or 0),
            "n_developing":    int(stats.get("n_developing") or 0),
            "n_decided":       int(stats.get("n_decided") or 0),
            # Hit rates stored as raw numbers (not strings with %) so the
            # chart can plot them numerically. None passes through as null.
            "hit_rate_2r":     stats.get("hit_rate_2r"),
            "hit_max_5pct":    stats.get("hit_max_5pct"),
            "hit_max_10pct":   stats.get("hit_max_10pct"),
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return _LOG_PATH
    except Exception:
        return None


def load_history(days: int = 90) -> list[dict]:
    """Return recent snapshots, deduped so at most ONE snapshot per date
    (the latest one for that date). Oldest first.

    Return empty list if the file doesn't exist or fails to parse. Reads
    the whole file, which is fine for the small sizes we accumulate
    (a year of daily scans is ~50 KB).
    """
    if not _LOG_PATH.exists():
        return []
    try:
        raw_lines = _LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []

    # Parse all lines, keep the latest per date (assumes lines are
    # written in chronological order, which they are — append-only).
    by_date: dict[str, dict] = {}
    for line in raw_lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        d = rec.get("date")
        if d:
            by_date[d] = rec  # later occurrence wins

    # Sort by date ascending
    sorted_records = [by_date[d] for d in sorted(by_date.keys())]

    if days > 0:
        return sorted_records[-days:]
    return sorted_records


def get_previous_snapshot(current_date_str: str) -> Optional[dict]:
    """Return the most recent snapshot BEFORE current_date_str, or None
    if no earlier snapshot exists. Used for the "vs previous scan" delta
    indicators on the tiles.

    'Previous' = closest date strictly less than current_date_str. Handles
    weekends and skipped scan days naturally — we just take the most
    recent prior entry regardless of gap length.
    """
    history = load_history(days=0)  # unbounded
    prior = [r for r in history if r.get("date") and r["date"] < current_date_str]
    return prior[-1] if prior else None
