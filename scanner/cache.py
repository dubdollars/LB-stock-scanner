"""
Scan-result cache.

Saves each scan as a timestamped JSON file under data/scans/, plus a
'latest.json' pointer for instant dashboard load. Keeps the most recent
30 scans, prunes older ones.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .core import ScanResult

# Directory layout: <project_root>/data/scans/
SCANS_DIR = Path(__file__).resolve().parent.parent / "data" / "scans"
LATEST_FILE = SCANS_DIR / "latest.json"
LATEST_SHORT_FILE = SCANS_DIR / "latest_short.json"
KEEP_LAST_N = 30

# Rate-limit guardrail — any Momentum scan producing fewer than this many
# tickers in the universe is almost certainly the result of yfinance
# rate-limiting (Yahoo throttles our IP after too many requests in a short
# window). Healthy scans in 2026 run ~3,200 tickers. Rate-limited scans
# collapse to 700-800 (the mid-cap subset that survives before Yahoo starts
# 429-ing us). Threshold set to 1500 — well above the rate-limited floor
# and well below normal counts, so a legitimate mid-scan yfinance outage
# vs a rate-limit is unambiguous.
#
# When a scan falls below this floor, save_scan() still archives the
# result as scan_YYYYMMDD_HHMMSS.json for debugging, but REFUSES to
# overwrite latest.json — so the Streamlit dashboard keeps showing
# yesterday's healthy data instead of getting corrupted with the
# rate-limited garbage.
#
# History: this happened three times in as many days (2026-06-30 02:08,
# 2026-06-30 02:16, and 2026-07-01 12:40/12:43) before we added this
# guardrail. Every time, the user's dashboard collapsed from ~3,246
# universe to ~700, requiring manual latest.json restoration or an 8-24
# hour rate-limit cooldown.
MIN_HEALTHY_UNIVERSE = 1500

# Momentum scan filenames match exactly scan_YYYYMMDD_HHMMSS.json — 8 digits,
# underscore, 6 digits. Use this regex to filter out other scan types
# (scan_quality_*, scan_mean_reversion_*, scan_short_*, scan_early_*) before
# pruning. See _prune_old() for the bug this prevents.
_MOMENTUM_SCAN_RE = re.compile(r"^scan_\d{8}_\d{6}\.json$")
_SHORT_SCAN_RE = re.compile(r"^scan_short_\d{8}_\d{6}\.json$")


def _ensure_dir() -> None:
    SCANS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically — never leaves a partial file.

    Writes to a temp file in the same directory, then os.replace() swaps
    it in. os.replace() is atomic on both Windows and POSIX when source
    and destination are on the same filesystem.

    Without this, an interrupted Streamlit rerun can leave latest.json
    half-written and the next scan load fails with JSONDecodeError. We
    saw this repeatedly during dev (user-reported 2026-06-06: blank
    Top Stocks page caused by truncated latest.json + latest_quality.json).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _serialize(result: ScanResult) -> dict:
    if is_dataclass(result):
        return asdict(result)
    return dict(result)  # type: ignore[arg-type]


def save_scan(result: ScanResult) -> tuple[Path, bool]:
    """Save scan to data/scans/scan_YYYYMMDD_HHMMSS.json (always) and
    to latest.json (only if the scan passes the rate-limit guardrail).

    Returns (out_path, latest_updated) where latest_updated is False when
    the guardrail rejected the scan. Caller (Streamlit UI) can use that
    to show a warning to the user.

    Both writes go through _atomic_write_text() so a crash mid-write
    leaves the prior file intact rather than truncated.

    Rate-limit guardrail: if the scan's universe_size is suspiciously
    small (< MIN_HEALTHY_UNIVERSE), we DO NOT overwrite latest.json —
    the dashboard keeps showing the previous healthy scan. This prevents
    a rate-limited scan from corrupting the user's working data.
    """
    _ensure_dir()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = SCANS_DIR / f"scan_{stamp}.json"
    payload = _serialize(result)
    body = json.dumps(payload, indent=2, default=str)

    # Always archive the raw scan — even rate-limited ones — for debugging
    # and post-mortem analysis. Just don't promote them to "latest".
    _atomic_write_text(out_path, body)

    universe_size = payload.get("universe_size", 0) or 0
    is_healthy = universe_size >= MIN_HEALTHY_UNIVERSE

    if is_healthy:
        _atomic_write_text(LATEST_FILE, body)
        # Snapshot the BUY ZONE cohort — only for healthy scans, so a
        # rate-limited scan can't pollute the cohort log with 3-ticker
        # placeholder rows. Silent no-op if no candidates carry BUY ZONE.
        try:
            from .buy_zone_cohort import append_buy_zone_snapshot  # type: ignore
            append_buy_zone_snapshot(
                candidates=payload.get("candidates", []) or [],
                market_signal=payload.get("market_signal", ""),
                scan_timestamp=payload.get("timestamp"),
            )
        except Exception:
            # Never let a cohort-log failure block the main scan-save path.
            pass
        # Append market-state snapshot to the append-only jsonl log.
        # Used for diffing distribution-day counts across scans and
        # explaining any change ("what day appeared/disappeared and why").
        try:
            from .dist_log import append_scan_snapshot as _append_state_snapshot  # type: ignore
            _append_state_snapshot(payload)
        except Exception:
            pass

    _prune_old()
    return out_path, is_healthy


def scan_looks_rate_limited(result_or_dict) -> bool:
    """Utility for UI code — returns True if the scan appears rate-limited.

    Accepts either a ScanResult dataclass or a plain dict from a loaded
    scan file. Threshold matches save_scan()'s guardrail.
    """
    if is_dataclass(result_or_dict):
        universe = getattr(result_or_dict, "universe_size", 0) or 0
    else:
        universe = (result_or_dict or {}).get("universe_size", 0) or 0
    return universe < MIN_HEALTHY_UNIVERSE


def load_latest_scan() -> Optional[dict]:
    """Return the most recent scan as a plain dict, or None if no scans exist."""
    _ensure_dir()
    if LATEST_FILE.exists():
        try:
            return json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    # Fallback: find newest scan_*.json
    scans = sorted(SCANS_DIR.glob("scan_*.json"), reverse=True)
    if scans:
        try:
            return json.loads(scans[0].read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def list_scans() -> list[dict]:
    """Return [{file, timestamp, candidates_count, market_signal}, ...] newest first."""
    _ensure_dir()
    out = []
    for path in sorted(SCANS_DIR.glob("scan_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "file": path.name,
                "timestamp": data.get("timestamp", ""),
                "candidates_count": len(data.get("candidates", [])),
                "market_signal": data.get("market_signal", ""),
                "mode": data.get("mode_label", ""),
            })
        except Exception:
            continue
    return out


def _prune_old() -> None:
    """Prune old Momentum scan files — keep only the most recent KEEP_LAST_N.

    CRITICAL: must filter to Momentum-only filename pattern before pruning.
    glob('scan_*.json') matches EVERY scan type (quality, mean_reversion,
    short, early). In reverse-alphabetical sort order Momentum scans
    (scan_YYYYMMDD_*) sort LAST because digits come before letters in ASCII,
    so an unfiltered prune deletes Momentum scans FIRST when total scans
    exceed the keep limit. This bug silently wiped historical Momentum scans
    and broke the Industry Rotation page's 1w/4w delta columns
    (user-reported 2026-06-17).
    """
    momentum_scans = sorted(
        (p for p in SCANS_DIR.glob("scan_*.json") if _MOMENTUM_SCAN_RE.match(p.name)),
        reverse=True,
    )
    for path in momentum_scans[KEEP_LAST_N:]:
        try:
            path.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Short-scan cache (parallel API to long-scan cache, separate files)
# ---------------------------------------------------------------------------

def save_short_scan(result) -> Path:
    """Save short-scan result to data/scans/scan_short_YYYYMMDD_HHMMSS.json
    + latest_short.json. Doesn't collide with long-scan cache.

    Atomic writes prevent half-written files (see _atomic_write_text docstring).
    """
    _ensure_dir()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = SCANS_DIR / f"scan_short_{stamp}.json"
    payload = _serialize(result)
    body = json.dumps(payload, indent=2, default=str)

    _atomic_write_text(out_path, body)
    _atomic_write_text(LATEST_SHORT_FILE, body)
    _prune_old_short()
    return out_path


def load_latest_short_scan() -> Optional[dict]:
    """Return the most recent short scan as a plain dict, or None."""
    _ensure_dir()
    if LATEST_SHORT_FILE.exists():
        try:
            return json.loads(LATEST_SHORT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    # Fallback: find newest scan_short_*.json
    scans = sorted(SCANS_DIR.glob("scan_short_*.json"), reverse=True)
    if scans:
        try:
            return json.loads(scans[0].read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def list_short_scans() -> list[dict]:
    """Newest-first metadata for past short scans."""
    _ensure_dir()
    out = []
    for path in sorted(SCANS_DIR.glob("scan_short_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "file": path.name,
                "timestamp": data.get("timestamp", ""),
                "candidates_count": len(data.get("candidates", [])),
                "spy_rsi": data.get("spy_rsi"),
                "qqq_rsi": data.get("qqq_rsi"),
            })
        except Exception:
            continue
    return out


def _prune_old_short() -> None:
    """Prune old short-scan files. Same Momentum-style fix: filter to
    short-scan-only pattern before pruning so we don't accidentally cull
    other scan types."""
    short_scans = sorted(
        (p for p in SCANS_DIR.glob("scan_short_*.json") if _SHORT_SCAN_RE.match(p.name)),
        reverse=True,
    )
    for path in short_scans[KEEP_LAST_N:]:
        try:
            path.unlink()
        except Exception:
            pass
