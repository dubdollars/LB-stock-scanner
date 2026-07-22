"""
Persistent scan snapshot log.

Every healthy Momentum scan appends a compact JSON line to data/scans/
market_state_log.jsonl capturing the distribution-day flag lists, the
market-state snapshot, and any FTD history. This gives us an audit
trail so when the count changes between scans, we can diff exactly
which day was added/removed instead of guessing from memory.

Format: newline-delimited JSON (jsonl), append-only. One line per scan.
Each line is a self-contained dict with:

  {
    "timestamp":      "2026-07-22T20:15:00Z",   # scan's own timestamp
    "sp_dist_dates":  ["2026-07-13", ...],
    "nq_dist_dates":  [...],
    "rut_dist_dates": [...],
    "market_state":   {"state": "RALLY_ATTEMPT", "rally_day": 3, ...},
  }

Read the file (or tail the last N lines) to see how distribution counts
and market state have moved over time.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH  = _REPO_ROOT / "data" / "scans" / "market_state_log.jsonl"


def append_scan_snapshot(scan_result) -> Optional[Path]:
    """Append one line to the market_state log capturing the distribution
    and state info from the given scan.

    Accepts either a ScanResult dataclass or a dict (already serialized).
    Silent no-op on any failure — we never want the log to block or
    corrupt a scan save.
    """
    try:
        if hasattr(scan_result, "to_dict"):
            payload = scan_result.to_dict()
        elif isinstance(scan_result, dict):
            payload = scan_result
        else:
            return None

        record = {
            "timestamp":      payload.get("timestamp", datetime.utcnow().isoformat() + "Z"),
            "sp_dist_dates":  payload.get("sp_dist_dates",  []) or [],
            "nq_dist_dates":  payload.get("nq_dist_dates",  []) or [],
            "rut_dist_dates": payload.get("rut_dist_dates", []) or [],
            "market_state":   payload.get("market_state",   {}) or {},
        }

        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return _LOG_PATH
    except Exception:
        return None


def load_recent_snapshots(n: int = 10) -> list[dict]:
    """Return the most recent N snapshots (newest last) from the log.

    Empty list if the log doesn't exist yet or fails to parse. Reads the
    whole file — fine for the small sizes we accumulate (a few years of
    daily scans is ~1 MB).
    """
    if not _LOG_PATH.exists():
        return []
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def diff_last_two() -> Optional[dict]:
    """Compare the two most recent scans and return which distribution
    dates were added/removed and any state-machine transition.

    Returns None if fewer than two snapshots exist. Useful for
    diagnosing "why did the count change" questions.
    """
    snaps = load_recent_snapshots(n=2)
    if len(snaps) < 2:
        return None
    prev, cur = snaps[0], snaps[1]

    def _diff(a: list, b: list) -> dict:
        sa, sb = set(a or []), set(b or [])
        return {
            "added":   sorted(sb - sa),
            "removed": sorted(sa - sb),
        }

    return {
        "prev_timestamp": prev.get("timestamp"),
        "cur_timestamp":  cur.get("timestamp"),
        "sp_diff":        _diff(prev.get("sp_dist_dates"),  cur.get("sp_dist_dates")),
        "nq_diff":        _diff(prev.get("nq_dist_dates"),  cur.get("nq_dist_dates")),
        "rut_diff":       _diff(prev.get("rut_dist_dates"), cur.get("rut_dist_dates")),
        "prev_state":     (prev.get("market_state") or {}).get("state"),
        "cur_state":      (cur.get("market_state")  or {}).get("state"),
    }
