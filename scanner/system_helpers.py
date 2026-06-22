"""
Platform helpers — currently just sleep prevention for long-running scans.

When the laptop sleeps mid-scan, Python pauses and yfinance loses its network
connections — the scan effectively stops and the user has to restart from
scratch. Calling SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
tells Windows "don't sleep while I'm working." Always paired with allow_sleep()
in a try/finally so normal power management resumes when the scan finishes.

No-op on non-Windows platforms (Linux/macOS Streamlit hosts) — the user is on
Windows, but if anyone runs this elsewhere the wrappers degrade gracefully.
"""
from __future__ import annotations

import sys


def keep_awake() -> None:
    """Tell the OS not to sleep while a scan is running. Windows-only; no-op elsewhere.

    Keeps the system awake but allows the display to turn off (a 25-minute
    Momentum scan doesn't need the screen lit and we don't want to waste battery).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
    except Exception:
        pass  # best-effort — fall back to OS defaults


def allow_sleep() -> None:
    """Restore normal power management after the scan finishes."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass
