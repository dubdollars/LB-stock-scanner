"""
Scan-result cache.

Saves each scan as a timestamped JSON file under data/scans/, plus a
'latest.json' pointer for instant dashboard load. Keeps the most recent
30 scans, prunes older ones.
"""

from __future__ import annotations

import json
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


def _ensure_dir() -> None:
    SCANS_DIR.mkdir(parents=True, exist_ok=True)


def _serialize(result: ScanResult) -> dict:
    if is_dataclass(result):
        return asdict(result)
    return dict(result)  # type: ignore[arg-type]


def save_scan(result: ScanResult) -> Path:
    """Save scan to data/scans/scan_YYYYMMDD_HHMMSS.json + latest.json."""
    _ensure_dir()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = SCANS_DIR / f"scan_{stamp}.json"
    payload = _serialize(result)

    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    LATEST_FILE.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    _prune_old()
    return out_path


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
    scans = sorted(SCANS_DIR.glob("scan_*.json"), reverse=True)
    for path in scans[KEEP_LAST_N:]:
        try:
            path.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Short-scan cache (parallel API to long-scan cache, separate files)
# ---------------------------------------------------------------------------

def save_short_scan(result) -> Path:
    """Save short-scan result to data/scans/scan_short_YYYYMMDD_HHMMSS.json
    + latest_short.json. Doesn't collide with long-scan cache."""
    _ensure_dir()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = SCANS_DIR / f"scan_short_{stamp}.json"
    payload = _serialize(result)

    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    LATEST_SHORT_FILE.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
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
    scans = sorted(SCANS_DIR.glob("scan_short_*.json"), reverse=True)
    for path in scans[KEEP_LAST_N:]:
        try:
            path.unlink()
        except Exception:
            pass
